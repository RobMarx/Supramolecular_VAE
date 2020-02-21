import multiprocessing
import os
from collections import OrderedDict

import dask
import dask.dataframe
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import sklearn.metrics
import torch
from dask.diagnostics import ProgressBar
from tqdm.autonotebook import tqdm

from . import utils
from .scscore import SCScorer

tqdm.pandas()


def fast_scscore(df, smiles_column, label='scscore', scorer=None):
    if scorer is None:
        scorer = SCScorer()
        scorer.restore()

    n_cores = multiprocessing.cpu_count()
    print('Calculating scscore on {} cores'.format(n_cores))
    dd = dask.dataframe.from_pandas(df, npartitions=n_cores ** 2)

    def get_score(x): return scorer.get_score_from_smi(x)[1]

    with ProgressBar():
        series = dd[smiles_column].apply(get_score, meta=(
            label, np.float32)).compute(scheduler='processes')
    return series


def plot_settings():
    sns.set_context('talk', font_scale=1.25)
    mpl.rcParams['figure.figsize'] = [12.0, 6.0]
    mpl.rcParams['lines.linewidth'] = 2.5


def save_figure(adir, name):
    filename = os.path.join(adir, name + '.png')
    plt.savefig(filename, dpi=300,
                bbox_inches='tight', transparent=True)
    filename = os.path.join(adir, name + '.svg')
    plt.savefig(filename, dpi=300,
                bbox_inches='tight', transparent=True)


def get_y_true(data, model):
    y_true = np.stack([t[2] for t in data])
    return model.y_scaler.inverse_transform(y_true)


def regression_statistics(y_true, y_pred, targets, prefix=''):
    results = []
    for index, col in enumerate(targets):
        result = OrderedDict({'label': col})
        result[prefix +
               'R2'] = sklearn.metrics.r2_score(y_true[:, index], y_pred[:, index])
        result[prefix + 'MAE'] = sklearn.metrics.mean_absolute_error(
            y_true[:, index], y_pred[:, index])
        result[prefix + 'RMSE'] = np.sqrt(
            sklearn.metrics.mean_squared_error(y_true[:, index], y_pred[:, index]))
        results.append(result)

    return pd.DataFrame(results)


def predict_properties(data, model, trainer, batch_size=64):
    n = len(data)
    n_loops = int(np.ceil(n / batch_size))
    y_true = []
    y_pred = []
    with torch.no_grad():
        for chunk in tqdm(utils.chunks(data, batch_size), total=n_loops, desc='Predicting'):
            x_tensor, mof_tensor, y_tensor, _ = trainer.get_collate_fn(
                model)(chunk)
            _, z_tensor = model.forward_encoder(x_tensor, mof_tensor)
            y_pred_tensor = model.z_to_y(z_tensor)
            # z.extend(z_tensor.cpu().numpy())
            y_pred_scaled = model.y_scaler.inverse_transform(
                y_pred_tensor.cpu().numpy())
            y_pred.extend(y_pred_scaled)
            y_true_scaled = model.y_scaler.inverse_transform(
                y_tensor.cpu().numpy())
            y_true.extend(y_true_scaled)

    y_pred = np.stack(y_pred)
    y_true = np.stack(y_true)
    return y_pred, y_true


def plot_mof_stats(df, mof_vocab, mof_columns):
    for col in mof_columns:
        print(utils.header_str(col))
        label2id = mof_vocab.get_label2id(col)
        weights = mof_vocab.weights[col]
        counts = {}
        values = df[col].tolist()
        labels, counts = np.unique(values, return_counts=True)
        counts = {label2id[l]: c for l, c in zip(labels, counts)}
        plt.bar(list(counts.keys()), list(counts.values()))
        plt.xticks(list(counts.keys()), labels, rotation=90)
        plt.show()
        indexes = list(range(len(weights)))
        plt.bar(indexes, weights)
        plt.xticks(list(label2id.values()), list(label2id.keys()), rotation=90)
        plt.show()


def get_generator_df(csv_file, smiles_column, use_duplicates, testing):
    df = pd.read_csv(csv_file)
    if smiles_column not in df.columns:
        df = df.rename(columns={'SMILES': smiles_column})

    if not use_duplicates:
        df = df.drop_duplicates(subset=smiles_column)
    if testing:
        index = df['id2mof'].drop_duplicates().index
        df = df.loc[index].reset_index()
        print(utils.header_str('Testing'))
    else:
        print(utils.header_str('Real run'))

    print('df shape: {}'.format(df.shape))
    print('df columns: {}'.format(df.columns.tolist()))
    return df


def get_mofdict(df, mof_type):
    if mof_type == 'id':
        mof_columns = ['id2mof']
    elif mof_type == 'cats':
        mof_columns = ['metal_node', 'organic_core', 'topology']
    elif mof_type == 'all':
        mof_columns = ['metal_node', 'organic_core', 'topology', 'id2mof']
    else:
        raise ValueError('{} not understood!'.format(mof_type))

    index = df['id2mof'].drop_duplicates().index
    sub_df = df.loc[index].sort_values(by='id2mof')
    mof2ids = OrderedDict()
    ids2mof = OrderedDict()
    for _, row in sub_df.iterrows():
        mof = (row['metal_node'], row['organic_core'], row['topology'])
        ids2mof[row['id2mof']] = mof
        mof2ids[mof] = row['id2mof']
    print('Found {} unique mofs'.format(len(mof2ids)))
    return ids2mof, mof2ids, mof_columns


def get_prop_df(csv_file, targets, mof2ids, testing, smiles_column, compute_scscore=False):
    mof_cols = ['metal_node', 'organic_core', 'topology']

    def valid_mof(x):
        return tuple(x) in mof2ids.keys()

    df = pd.read_csv(csv_file)
    if testing:
        df = df.sample(n=1000).reset_index(drop=True)
        print(utils.header_str('Testing'))
    else:
        print(utils.header_str('Real run'))

    if compute_scscore:
        scorer = SCScorer()
        scorer.restore()
        df['scscore'] = df[smiles_column].progress_apply(lambda x: scorer.get_score_from_smi(x)[1])

    assert all([t in df.columns.tolist() for t in targets]), f'{targets} not in df!'

    n_remove = len(df) - len(df.query('mask'))
    print('Removed {} datapoints due to mask.'.format(n_remove))
    df = df.query('mask').reset_index(drop=True)

    valid_mofs_list = df[mof_cols].apply(valid_mof, axis=1).tolist()
    n_remove = len(df) - sum(valid_mofs_list)
    df = df[valid_mofs_list].reset_index(drop=True)

    df['id2mof'] = df[mof_cols].apply(lambda x: mof2ids[tuple(x)], axis=1)
    print('Removed {} datapoints due non-valid mof (mof2ids).'.format(n_remove))

    # Specific changes to properties
    # df['co2n2_selectivity'] = df['co2n2_selectivity'].apply(
    #    lambda x: x if x < 50 else 50.0)
    # df['co2ch4_selectivity'] = df['co2ch4_selectivity'].apply(
    #    lambda x: x if x < 50 else 50.0)
    n_remove = len(df)
    df = df[df['co2n2_selectivity'] < 200.0]
    df = df[df['co2ch4_selectivity'] < 200.0]
    df = df.reset_index(drop=True)
    n_remove -= len(df)
    print('Removed {} datapoints due to high selectivity.'.format(n_remove))

    return df