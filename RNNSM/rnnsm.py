import torch
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from scipy.integrate import trapz
import numpy as np


class RNNSM(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        hidden_size = cfg.hidden_size
        self.lstm = nn.LSTM(cfg.input_size, cfg.lstm_hidden_size, batch_first=True)
        cat_sizes = cfg.cat_sizes
        emb_dims = cfg.emb_dims
        self.embeddings = nn.ModuleList([nn.Embedding(cat_size + 1, emb_dim, padding_idx=0) for cat_size, emb_dim in zip(cat_sizes, emb_dims)])

        total_emb_length = sum(emb_dims)
        self.input_dense = nn.Linear(total_emb_length + cfg.n_num_feats, cfg.input_size)
        self.dropout = nn.Dropout(cfg.dropout)
        self.output_dense = nn.Linear(cfg.hidden_size, 1)

        self.hidden = nn.Linear(cfg.lstm_hidden_size, cfg.hidden_size)
        self.w = cfg.w
        self.time_scale = cfg.time_scale

        self.integration_steps = cfg.integration_steps

    def forward(self, cat_feats, num_feats, lengths):
        x = torch.zeros((cat_feats.size(0), cat_feats.size(1), 0)).to(cat_feats.device)
        for i, emb in enumerate(self.embeddings):
            x = torch.cat([x, emb(cat_feats[:, :, i])], axis=-1)
        x = torch.cat([x, num_feats], axis=-1)
        x = self.dropout(torch.tanh(self.input_dense(x)))
        x = pack_padded_sequence(x, lengths=lengths, batch_first=True, enforce_sorted=False)
        h_j, _ = self.lstm(x)
        h_j, _ = pad_packed_sequence(h_j, batch_first=True)
        h_j = self.dropout(torch.tanh(self.hidden(h_j)))
        o_j = self.output_dense(h_j).squeeze()
        return o_j


    def compute_loss(self, deltas, padding_mask, ret_mask, o_j):
        deltas_scaled = deltas * self.time_scale
        p = o_j + self.w * deltas_scaled
        common_term = -(torch.exp(o_j) - torch.exp(p)) / self.w * ~padding_mask
        common_term = common_term.sum() / torch.sum(~padding_mask)
        ret_term = (-p * ret_mask).sum() / torch.sum(ret_mask)
        return common_term + ret_term


    def predict_standard(self, o_j, t_j, lengths):
        with torch.no_grad():
            last_o_j = o_j[torch.arange(o_j.size(0)), lengths - 1]
            last_t_j = t_j[torch.arange(t_j.size(0)), lengths - 1]
            deltas = torch.arange(0, 1000 * self.time_scale, self.time_scale).to(o_j.device)

            s_deltas = self._s_t(last_o_j, deltas, broadcast_deltas=True)
        return last_t_j.cpu().numpy() + trapz(s_deltas.cpu(), deltas_scaled.cpu()) / self.time_scale


    def predict(self, o_j, t_j, lengths, pred_start):
        with torch.no_grad():
            batch_size = o_j.size(0)
            last_o_j = o_j[torch.arange(batch_size), lengths - 1]
            last_t_j = t_j[torch.arange(batch_size), lengths - 1]

            t_til_start = (pred_start - last_t_j) * self.time_scale

            s_t_s = self._s_t(last_o_j, t_til_start)
            s_t_s = torch.clamp(s_t_s, 1e-3, 1e0)

            zeros = torch.zeros(last_t_j.size()).to(last_t_j.device)
            deltas = torch.arange(0, self.integration_steps * self.time_scale, self.time_scale).to(o_j.device)
            s_deltas = self._s_t(last_o_j, deltas, broadcast_deltas=True)

            preds = np.zeros(batch_size)
            for i in range(batch_size):
                ith_t_s, ith_s_t_s = t_til_start[i], s_t_s[i]
                ith_s_deltas = s_deltas[i]
                pred_delta = trapz(ith_s_deltas[deltas < ith_t_s].cpu(), deltas[deltas < ith_t_s].cpu())
                pred_delta += trapz(ith_s_deltas[deltas >= ith_t_s].cpu(), deltas[deltas >= ith_t_s].cpu()) / ith_s_t_s.item()
                preds[i] = last_t_j[i].cpu().numpy() + pred_delta / self.time_scale

        return preds


    def _s_t(self, last_o_j, deltas, broadcast_deltas=False):
        if broadcast_deltas:
            out = torch.exp(torch.exp(last_o_j)[:, None] / self.w - \
                    torch.exp(last_o_j[:, None] + self.w * deltas[None, :]) / self.w)
        else:
            out = torch.exp(torch.exp(last_o_j) / self.w - \
                    torch.exp(last_o_j + self.w * deltas) / self.w)
        return out.squeeze()


    def save_model(self, path):
        torch.save({'lstm': self.lstm.state_dict(),
                    'embeddings': self.embeddings.state_dict(),
                    'input_dense': self.input_dense.state_dict(),
                    'output_dense': self.output_dense.state_dict(),
                    'hidden': self.hidden.state_dict()
                    }, path)


    def load_model(self, path):
        params = torch.load(path)
        self.lstm.load_state_dict(params['lstm'])
        self.embeddings.load_state_dict(params['embeddings'])
        self.input_dense.load_state_dict(params['input_dense'])
        self.output_dense.load_state_dict(params['output_dense'])
        self.hidden.load_state_dict(params['hidden'])
