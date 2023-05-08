from sklearn.model_selection import KFold
import numpy as np
import pandas as pd
from tools import file_writer
from pathlib import Path
import config as cfg
from tools.feature_selectors import NoneSelector, LowVar, SelectKBest10, RegMRMR10 #RFE10,RFE20, SelectKBest20, RegMRMR20
from tools.regressors import Cph, CphRidge, CphLasso, CphElastic, RSF, CoxBoost, WeibullAFT,  XGBTree # XGBDart, XGBLinear
from tools import file_reader
from tools import data_ETL
from sklearn.model_selection import train_test_split
from xgbse.metrics import approx_brier_score
from sklearn.model_selection import RandomizedSearchCV
from lifelines.utils import concordance_index
from sksurv.metrics import concordance_index_censored
from time import time
import math

N_REPEATS = 3
N_SPLITS = 3
N_ITER = 3
N_BOOT = 2

def main():

    cov, boot, info_pack = file_reader.FileReader().read_data_xjtu()
    
#    df_surv = data_ETL.DataETL().make_covariates(df)
    X, y = data_ETL.DataETL().make_surv_data_sklS(cov, boot, info_pack, N_BOOT)
#    X_2, y_2 = data_ETL.DataETL().make_surv_data_pyS(df_surv)

    models = [Cph, CphRidge, CphLasso, CphElastic, WeibullAFT, RSF, CoxBoost, XGBTree] #, WeibullAFT , XGBDart, XGBLinear,
    ft_selectors = [NoneSelector, LowVar, SelectKBest10, RegMRMR10] #RFE10, RFE20, SelectKBest20, RegMRMR20

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.1, random_state=0)
    T1, HOS = (X_train, y_train), (X_test, y_test)

    print(f"Started evaluation of {len(models)} models/{len(ft_selectors)} ft selectors/{len(T1[0])} total samples")
    for model_builder in models:
        model_name = model_builder.__name__
        model_results = pd.DataFrame()
        for ft_selector_builder in ft_selectors:
            ft_selector_name = ft_selector_builder.__name__
            print ("ft_selector name: ", ft_selector_name )
            print ("model_builder name: ", model_name )
            for n_repeat in range(N_REPEATS):
                kf = KFold(n_splits=N_SPLITS, shuffle=True, random_state=n_repeat)
                for train, test in kf.split(T1[0], T1[1]):
                    split_start_time = time()
                    # Split data
                    ti = (T1[0].iloc[train], T1[1][train])
                    cvi = (T1[0].iloc[test], T1[1][test])

                    # Get current model and ft selector
                    if ft_selector_name == "NoneSelector":
                        ft_selector_print_name = f"({ft_selectors.index(ft_selector_builder)+1}) None"
                    else:
                        ft_selector_print_name = f"({ft_selectors.index(ft_selector_builder)+1}) {ft_selector_name}"
                    model_print_name = f"({models.index(model_builder)+1}) {model_name}"

                    # Create model instance and find best features
                    get_best_features_start_time = time()
                    model = model_builder().get_estimator()
                    model_class_name = model.__class__.__name__

                    if ft_selector_name in ["RegMRMR10", "RegMRMR20"]:
                        y_ti_mrmr = np.array([x[0] for x in ti[1]], float)
                        ft_selector = ft_selector_builder(ti[0], y_ti_mrmr, estimator=model)
                    elif (model_name == 'WeibullAFT' and ft_selector_name in ["NoneSelector", "LowVar", "RFE10", "RFE20"]):
                        # No support for WeibullAFT and some selectors, so skip runs
                        c_index, brier_score = np.nan, np.nan
                        get_best_features_time, get_best_params_time, model_train_time = np.nan, np.nan, np.nan
                        model_ci_inference_time, model_bs_inference_time = np.nan, np.nan
                        t_total_split_time = np.nan
                        best_params, selected_fts = {}, []
                        res_sr = pd.Series([model_print_name, ft_selector_print_name, n_repeat, c_index, brier_score,
                                            get_best_features_time, get_best_params_time, model_train_time,
                                            model_ci_inference_time, model_bs_inference_time, t_total_split_time,
                                            best_params, selected_fts],
                                            index=["ModelName", "FtSelectorName", "NRepeat", "CIndex", "BrierScore",
                                                   "TBestFeatures", "TBestParams", "TModelTrain",
                                                   "TModelCIInference", "TModelBSInference", "TTotalSplit",
                                                   "BestParams", "SelectedFts"])
                        model_results = pd.concat([model_results, res_sr.to_frame().T], ignore_index=True)
                        continue
                    elif model_name == "WeibullAFT" and ft_selector_name in ["RegMRMR10", "RegMRMR20"]:
                        y_ti_mrmr = np.array([x[0] for x in ti[1]], float)
                        ft_selector = ft_selector_builder(ti[0], y_ti_mrmr, estimator=model.lifelines_model)
                    elif model_class_name == "XGBRegressor" and ft_selector_name in ["RFE10", "RFE20"]:
                        y_ti_xgb = [x[1] if x[0] else -x[1] for x in ti[1]]
                        ft_selector = ft_selector_builder(ti[0], y_ti_xgb, estimator=model)
                    else:
                        ft_selector = ft_selector_builder(ti[0], ti[1], estimator=model)
                    selected_fts = ft_selector.get_features()
                    ti_new =  (ti[0].loc[:, selected_fts], ti[1])
                    cvi_new = (cvi[0].loc[:, selected_fts], cvi[1])
                    get_best_features_time = time() - get_best_features_start_time

                    print ("Selected features: ", selected_fts)

                    lower, upper = np.percentile(ti_new[1][ti_new[1].dtype.names[1]], [10, 90])
                    times = np.arange(math.ceil(lower), math.floor(upper+1))

                    # Find hyperparams via CV
                    get_best_params_start_time = time()
                    space = model_builder().get_tuneable_params()
                    if model_name == 'WeibullAFT':
                        wf = model()
                        search = RandomizedSearchCV(wf, space, n_iter=N_ITER, cv=N_SPLITS, random_state=0)
                        x_ti_wf = pd.concat([ti_new[0].reset_index(drop=True),
                                            pd.DataFrame(ti_new[1]['Event'], columns=['Event'])], axis=1)
                        y_ti_wf = np.array([x[1] for x in ti_new[1]], float)
                        search.fit(x_ti_wf, y_ti_wf)
                    elif model_class_name == "XGBRegressor":
                        search = RandomizedSearchCV(model, space, n_iter=N_ITER, cv= N_SPLITS, random_state=0)
                        y_ti_xgb = [x[1] if x[0] else -x[1] for x in ti_new[1]]
                        search.fit(ti_new[0], y_ti_xgb)
                    else:
                        search = RandomizedSearchCV(model, space, n_iter=N_ITER, cv= N_SPLITS, random_state=0)
                        search.fit(ti_new[0], ti_new[1])
                    best_params = search.best_params_
                    get_best_params_time = time() - get_best_params_start_time

                    # Train on train set TI with new params
                    model_train_start_time = time()
                    if model_name == "WeibullAFT":
                        model = search.best_estimator_
                        model.fit(x_ti_wf, y_ti_wf)
                    elif model_class_name == "XGBRegressor":
                        model = search.best_estimator_
                        model.fit(ti_new[0], y_ti_xgb)
                    else:
                        model = search.best_estimator_
                        model.fit(ti_new[0], ti_new[1])
                    model_train_time = time() - model_train_start_time

                    # Get C-index scores from current fold CVI
                    model_ci_inference_start_time = time()
                    if model_name == "WeibullAFT":
                        x_cvi_wf = pd.concat([cvi_new[0].reset_index(drop=True),
                                              pd.DataFrame(cvi_new[1]['Event'],
                                                           columns=['Event'])], axis=1)
                        preds = model.predict(x_cvi_wf)
                        c_index = concordance_index(cvi[1]['Survival_time'], preds, cvi[1]['Event'])
                    else:
                        preds = model.predict(cvi_new[0])
                        c_index = concordance_index_censored(cvi[1]['Event'], cvi[1]['Survival_time'], preds)[0]
                    model_ci_inference_time = time() - model_ci_inference_start_time

                    # Get BS scores from current fold CVI
                    model_bs_inference_start_time = time()
                    if model_name == "WeibullAFT":
                        model_instance = model.lifelines_model
                        # lower, upper = np.percentile(cvi_new[1][cvi_new[1].dtype.names[1]], [10, 90])
                        # times = np.arange(lower, upper+1)
                        surv_prob = model_instance.predict_survival_function(cvi_new[0]).T
                        brier_score = approx_brier_score(cvi_new[1], surv_prob)
                    elif model_class_name == "XGBRegressor":
                        brier_score = np.nan
                    else:
                        # lower, upper = np.percentile(cvi_new[1][cvi_new[1].dtype.names[1]], [10, 90])
                        # times = np.arange(lower, upper)
                        surv_probs = pd.DataFrame(np.row_stack([fn(times)
                                                                for fn in model.predict_survival_function(cvi_new[0])]))
                        brier_score = approx_brier_score(cvi_new[1], surv_probs)
                    model_bs_inference_time = time() - model_bs_inference_start_time

                    t_total_split_time = time() - split_start_time
                    print(f"Evaluated {model_print_name} - {ft_selector_print_name}" + \
                          f" - CI={round(c_index, 3)} - BS={round(brier_score, 3)} - T={round(t_total_split_time, 3)}")

                    # Record results
                    res_sr = pd.Series([model_print_name, ft_selector_print_name, n_repeat, c_index, brier_score,
                                        get_best_features_time, get_best_params_time, model_train_time,
                                        model_ci_inference_time, model_bs_inference_time, t_total_split_time,
                                        best_params, selected_fts],
                                        index=["ModelName", "FtSelectorName", "NRepeat", "CIndex", "BrierScore",
                                               "TBestFeatures", "TBestParams", "TModelTrain",
                                               "TModelCIInference", "TModelBSInference", "TTotalSplit",
                                               "BestParams", "SelectedFts"])
                    model_results = pd.concat([model_results, res_sr.to_frame().T], ignore_index=True)

        # Save model results
        file_name = f"{model_name}_results.csv"
        model_results.to_csv("data/logs/" + file_name)

if __name__ == "__main__":
    main()