import sys
sys.path.append('../data')

from data.ATM.dataset import get_ATM_train_val_loaders
import argparse
import os
from hydra.experimental import compose, initialize
from RNNSM.rnnsm import RNNSM
from RMTPP.rmtpp import RMTPP
from torch import optim
from collections import defaultdict
import torch
import numpy as np

from sklearn.metrics import roc_auc_score, recall_score

activity_start = 16000
prediction_start = 16400
prediction_end = 16500


def calc_rmse(predicted, target):
    predicted = predicted[target != -1]
    target = target[target != -1]
    return np.sqrt(np.mean((predicted - target) ** 2))


def calc_recall(predicted, target, prediction_end):
    y_pred = predicted > prediction_end
    y_true = target == -1
    return recall_score(y_true, y_pred)


def calc_auc(predicted, target, prediction_end):
    y_pred = predicted > prediction_end
    y_true = target == -1
    return roc_auc_score(y_true, y_pred)


def validate(val_loader, model, prediction_start, prediction_end, device):
    all_preds = []
    all_targets = []

    is_rnnsm = isinstance(model, RNNSM)

    with torch.no_grad():
        for timestamps, cat_feats, num_feats, targets, lengths in val_loader:
            if is_rnnsm:
                o_j = model(cat_feats.to(device), num_feats.to(device), lengths)
                preds = model.predict(o_j, timestamps, lengths, prediction_start)
            else:
                o_j, _ = model(cat_feats.to(device), num_feats.to(device), lengths)
                preds = model.predict(o_j, timestamps, lengths)
            targets = targets.numpy()

            all_preds.extend(preds.tolist())
            all_targets.extend(targets.tolist())

    all_preds = np.array(all_preds)
    all_targets = np.array(all_targets)
    return calc_rmse(all_preds, all_targets), \
        calc_recall(all_preds, all_targets, prediction_end), \
        calc_auc(all_preds, all_targets, prediction_end)


def train(train_loader, val_loader, model, optimizer, path, cfg, device):
    train_metrics = defaultdict(list)
    val_metrics = defaultdict(list)

    is_rnnsm = isinstance(model, RNNSM)

    for epoch in range(cfg.training.n_epochs):
        print(f'Epoch {epoch+1}/{cfg.training.n_epochs}....')
        losses = []
        model.train()

        for timestamps, cat_feats, num_feats, padding_mask, return_mask, lengths in train_loader:
            time_deltas = timestamps[:, 1:] - timestamps[:, :-1]
            if is_rnnsm:
                o_j = model(cat_feats.to(device), num_feats.to(device), lengths)
                loss = model.compute_loss(time_deltas, padding_mask, return_mask, o_j)
            else:
                o_j, y_j = model(cat_feats.to(device), num_feats.to(device), lengths)
                loss = model.compute_loss(time_deltas, padding_mask, o_j, y_j, cat_feats)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses.append(loss.item())

        train_metrics['loss'].append(np.mean(losses))
        print("Loss:", train_metrics['loss'][-1])

        model.eval()
        rmse, recall, auc = validate(val_loader,
            model,
            prediction_start,
            prediction_end,
            device)
        val_metrics['rmse'].append(rmse)
        val_metrics['recall'].append(recall)
        val_metrics['auc'].append(auc)
        print(f'Validation: '
              f'RMSE: {val_metrics["rmse"][-1]},\t'
              f'Recall: {val_metrics["recall"][-1]},\t'
              f'AUC: {val_metrics["auc"][-1]} ')
    torch.save(model.state_dict(), path)
    return train_metrics


def main():
    parser = argparse.ArgumentParser()


    parser.add_argument('--model',
            type=str,
            default='rmtpp',
            help='Name of the model to train. Either "rmtpp" or "rnnsm"')

    parser.add_argument('-d', '--dataset',
            type=str,
            default='ATM',
            help='Dataset to use. Allowed values: ["ATM"]')

    parser.add_argument('-p', '--model_path',
            type=str,
            default='./model.pth',
            help='Path where to save the model.')

    args = parser.parse_args()
    assert(args.model in ['rnnsm', 'rmtpp'], 'Invalid model name')

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    initialize(config_path=".")
    cfg = compose(config_name="config.yaml")

    model_class = RNNSM if args.model == 'rnnsm' else RMTPP
    model_cfg = cfg.rnnsm if args.model == 'rnnsm' else cfg.rmtpp
    max_seq_len = cfg.rnnsm.max_seq_len if args.model == 'rnnsm' else \
        cfg.rmtpp.max_seq_len
    train_loader, val_loader = get_ATM_train_val_loaders(model=args.model,
                                 activity_start=activity_start,
                                 prediction_start=prediction_start,
                                 prediction_end=prediction_end,
                                 path='data/ATM/train_day.csv',
                                 batch_size=cfg.training.batch_size,
                                 max_seq_len=max_seq_len)

    model = model_class(model_cfg).to(device)
    optimizer = optim.Adam(model.parameters(), lr=cfg.training.lr)
    train(train_loader, val_loader, model, optimizer, args.model_path, cfg, device)


if __name__ == '__main__':
    main()