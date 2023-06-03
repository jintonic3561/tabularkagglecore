# -*- coding: utf-8 -*-
"""
Created on Fri Mar 17 07:10:20 2023

@author: jintonic
"""


import pandas as pd
import numpy as np
import time
import joblib
import json
import pickle
import os
from mlutil.util import mlflow
from mlutil.util.notifier import slack_notify, SlackChannel
from mlutil.features import ABSFeatureGenerator
from mlutil.mlbase import MLBase

class ABSCallable:
    data_dir = './data/'
    
    def __init__(self):
        pass
    
    def __call__(self, df: pd.DataFrame, cache=False) -> pd.DataFrame:
        if cache:
            return self._cache(self.main)(df)
        else:
            return self.main(df)
        
    def main(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError()
        
    def _cache(self, func):
        # Note: not working when use decorator
        dir_ = f'{self.data_dir}joblib/{self.__class__.__name__}/'
        return joblib.Memory(dir_).cache(func, verbose=5)



class ABSDataFetcher(ABSCallable):
    def __call__(self, dry_run: bool=False, cache=False) -> pd.DataFrame:
        if cache:
            return self._cache(self.main)(dry_run)
        else:
            return self.main(dry_run)
        
    def main(self, dry_run: bool):
        raise NotImplementedError()



class ABSDataPreprocessor(ABSCallable):
    pass



def init_preprocessor(*args, cache=False):
    def _apply(df):
        for processor in args:
            df = processor(df)
        return df
    
    if cache:
        memory = lambda f: joblib.Memory('./data/').cache(f, verbose=0)
        return memory(_apply)
    else:
        return _apply
    


class ABSDataPostprocessor(ABSCallable):
    def __init__(self, save_dir):
        self.save_dir = save_dir
        self._init_dir()
        
    def main(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError()
    
    def save(self, processor):
        path = os.path.join(self.save_dir, self._get_file_name())
        with open(path, 'wb') as f:
            pickle.dump(processor, f)
    
    def load(self, path):
        with open(path, mode='rb') as f:
            return pickle.load(f)
    
    def _init_dir(self):
        try:
            if not os.path.exists(self.save_dir):
                os.makedirs(self.save_dir)
        # Note: kaggle notebook用
        except OSError:
            pass
    
    def _get_file_name(self):
        return self.__class__.__name__.lower() + '.pickle'



class ABSDataSplitter:
    def __init__(self, n_splits=5):
        self.n_splits = n_splits
    
    def train_test_split(self, df: pd.DataFrame) -> tuple:
        raise NotImplementedError()
        
    def cv_split(self, df: pd.DataFrame) -> tuple:
        '''
        Parameters
        ----------
        df : pd.DataFrame
        
        Yields
        -------
        train: pd.DataFrame, valid: pd.DataFrame
        '''
        raise NotImplementedError()



class ABSSubmitter:
    data_dir = './data/'
    competition_name = ''
    experiment_name = ''
    
    def __init__(self, 
                 data_fetcher: ABSDataFetcher,
                 data_preprocessor: ABSDataPreprocessor,
                 feature_generator: ABSFeatureGenerator,
                 data_splitter: ABSDataSplitter,
                 data_postprocessor: ABSDataPostprocessor,
                 model: MLBase,
                 submission_comment: str):
        '''
        Parameters
        ----------
        data_fetcher: ABSDataFetcher
        data_preprocessor: ABSDataPreprocessor
        feature_generator: ABSFeatureGenerator
        data_splitter: ABSDataSplitter
        data_postprocessor: ABSDataPostprocessor
        model: ABSModel, MLBase
        submission_comment: str
            The Message for submission.
        '''
        
        if not self.competition_name :
            raise ValueError('competition_name must be specified.')
        if not self.experiment_name:
            raise ValueError('experiment_name must be specified.')
        
        self.data_fetcher = data_fetcher
        self.data_preprocessor = data_preprocessor
        self.feature_generator = feature_generator
        self.data_splitter = data_splitter
        self.data_postprocessor = data_postprocessor
        self.model = model
        self.submission_comment = submission_comment
        self.api = self._init_kaggle_api()
    
    def get_submit_data(self, test: pd.DataFrame, cv_averaging: bool=True) -> pd.DataFrame:
        raise NotImplementedError()
        
    def validate_submit_data(self, sub):
        raise NotImplementedError()
        
    def get_experiment_params(self):
        raise NotImplementedError()
    
    def make_submission(self, 
                        retrain_all_data: bool=False,
                        save_model: bool=True,
                        dry_run: bool=False, 
                        return_only: bool=False):
        data = self._process_data(dry_run=dry_run)
        train, test = self.data_splitter.train_test_split(data)
        res = self._train_and_evaluate(train,
                                       retrain_all_data=retrain_all_data,
                                       save_model=save_model)
        sub = self.get_submit_data(test)
        self.validate_submit_data()
        
        if not dry_run:
            if return_only:
                return sub, res
            else:
                self._submit(sub)
                time.sleep(15)
                params = self.get_experiment_params()
                self._save_experiment(res.metrics, params=params)
        else:
            breakpoint()
            
    def _process_data(self, dry_run: bool):
        data = self.data_fetcher(dry_run=dry_run)
        data = self.data_preprocessor(data)
        data = self.feature_generator(data)
        data = self.data_postprocessor(data)
        return data
    
    def _train_and_evaluate(self, 
                            train: pd.DataFrame, 
                            retrain_all_data: bool=False,
                            save_model: bool=True) -> list:
        fold_generator = self.data_splitter.cv_split(train)
        res = self.model.cv(fold_generator, save_model=save_model and not retrain_all_data)
        if retrain_all_data:
            self.model.fit(train, save_model=save_model)
        return res
    
    def _submit(self, test: pd.DataFrame):
        file_name = f'{self.data_dir}submission.csv'
        test.to_csv(file_name, index=False)
        self.api.competition_submit(file_name=file_name,
                                    message=self.submission_comment,
                                    competition=self.competition_name)
    
    def _init_kaggle_api(self) -> any:
        # kaggle notebook上で失敗するため
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi
            api = KaggleApi()
            api.authenticate()
            return api
        except OSError:
            return None
    
    def _get_public_score(self) -> float:
        sub = self.api.competitions_submissions_list(self.competition_name)
        sub = pd.DataFrame(sub)
        sub['date'] = pd.to_datetime(sub['date'])
        score = sub.sort_values('date', ascending=False)['publicScoreNullable'].iloc[0]
        score = float(score) if score else np.nan
        return score
    
    def _save_experiment(self, cv_metrics: list, params: dict):
        mean = np.array(cv_metrics).mean()
        std = np.array(cv_metrics).std()
        sharpe = self._calc_sharpe(mean, std)
        public_score = self._get_public_score()
        metrics = {'cv_mean': mean,
                   'cv_std': std,
                   'cv_sharpe': sharpe,
                   'public_score': public_score}
        mlflow.run(experiment_name=self.experiment_name,
                   run_name=self.submission_comment,
                   params=params,
                   metrics=metrics,
                   artifact_paths=[self.model.model_dir])
        message = f'experiment finished. metrics:\n{json.dumps(metrics)}'
        slack_notify(message, channel=SlackChannel.regular)
        print(f'CV metrics: {[round(i, 4) for i in cv_metrics]}')
        print(f'mean: {round(mean, 4)}, std: {round(std, 4)}, sharpe: {round(sharpe, 4)}')
        
        
    def _calc_sharpe(self, mean, std):
        return mean / (std + 1)
    
    def _cache(self, func):
        # Note: not working when use decorator
        dir_ = f'{self.data_dir}joblib/{self.__class__.__name__}/'
        return joblib.Memory(dir_).cache(func, verbose=5)
    


# TODO: cvの返り値でcv_predictionsを扱うことにしたので、多分リファクタ必要
class EnsembleSubmitter(ABSSubmitter):
    def __init__(self, *submitters):
        self.submitters = submitters
        self.submission_comment = submitters[0].submission_comment
        self.api = self._init_kaggle_api()
        self.pred_col = submitters[0].model.pred_col
        
        if not self.pred_col:
            raise ValueError('pred_col must be specified.')
    
    def calc_ensembled_metrics(self, ensembled_preds):
        raise NotImplementedError()
        
    def make_submission(self, dry_run=False):
        sub, metrics = self._ensemble(dry_run=dry_run)
        self._validate_submit_data(sub)
        if not dry_run:
            self._submit(sub)
            time.sleep(15)
            params = {'model': 'Ensemble', 
                      'model_num': len(self.submitters), 
                      **self._get_model_names()}
            self._save_experiment(metrics, params=params)
        else:
            breakpoint()
        
    def _ensemble(self, dry_run):
        result = [i.make_submission(dry_run=dry_run, return_only=True) for i in self.submitters]
        each_sub = [i[0] for i in result]
        sub = each_sub[0].copy()
        sub[self.pred_col] = self._ensemble_predictions(each_sub)
        
        each_cv = [i.model.cv_predictions for i in self.submitters]
        each_cv = list(np.array(each_cv, dtype=object).T)
        each_cv = list(map(self._shape_cv_predictions, each_cv))
        apply = lambda each_df: self._ensemble_predictions(each_df)
        cv_preds = list(map(apply, each_cv))
        ensembled_cv = []
        for i in range(len(each_cv)):
            df = each_cv[i][0].copy()
            df[self.pred_col] = cv_preds[i]
            ensembled_cv.append(df)
        
        metrics = self._calc_metrics(ensembled_cv)
        return sub, metrics
    
    def _ensemble_predictions(self, each_df):
        preds = [i[self.pred_col].values for i in each_df]
        preds = np.stack(preds).mean(axis=0)
        return preds
    
    def _shape_cv_predictions(self, each_df):
        '''
        各モデルで必要ラグが違ったりして行数が違う場合があるので、必要に応じて最小のものに合わせる。
        '''    
        return each_df
    
    def _get_model_names(self):
        name = lambda s: s.model.__class__.__name__
        return {f'model_{i}': name(self.submitters[i]) for i in range(len(self.submitters))}    




