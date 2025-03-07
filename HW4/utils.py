import re
from collections import Counter
from tqdm import tqdm
import json
from torch.utils.data import Dataset, DataLoader
import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import math
from operator import attrgetter

RETOK = re.compile(r'\w+|[^\w\s]|\n', re.UNICODE)

class ChatDictionary(object):

    """
    Simple dict loader
    """

    def __init__(self, dict_file_path):
        self.word2ind = {}  # word:index
        self.ind2word = {}  # index:word
        self.counts = {}  # word:count

        dict_raw = open(dict_file_path, 'r').readlines()

        for i, w in enumerate(dict_raw):
            _word, _count = w.strip().split('\t')
            if _word == '\\n':
                _word = '\n'
            self.word2ind[_word] = i
            self.ind2word[i] = _word
            self.counts[_word] = _count

    def t2v(self, tokenized_text):
        return [self.word2ind[w] if w in self.counts else self.word2ind['__unk__'] for w in tokenized_text]

    def v2t(self, list_ids):
        return ' '.join([self.ind2word[i] for i in list_ids])

    def pred2text(self, tensor):
        result = []
        for i in range(tensor.size(0)):
            if tensor[i].item() == '__end__' or tensor[i].item() == '__null__':  # null is pad
                break
            else:
                result.append(self.ind2word[tensor[i].item()])
        return ' '.join(result)

    def __len__(self):
        return len(self.counts)


class ChatDataset(Dataset):
    """
    Json dataset wrapper
    """

    def __init__(self, dataset_file_path, dictionary, dt='train'):
        super().__init__()

        json_text = open(dataset_file_path, 'r').readlines()
        self.samples = []

        for sample in tqdm(json_text):
            sample = sample.rstrip()
            sample = json.loads(sample)
            _inp_toked = RETOK.findall(sample['text'])
            _inp_toked_id = dictionary.t2v(_inp_toked)

            sample['text_vec'] = torch.tensor(_inp_toked_id, dtype=torch.long)

            # train and valid have different key names for target
            if dt == 'train':
                _tar_toked = RETOK.findall(sample['labels'][0]) + ['__end__']
            elif dt == 'valid':
                _tar_toked = RETOK.findall(sample['eval_labels'][0]) + ['__end__']

            _tar_toked_id = dictionary.t2v(_tar_toked)

            sample['target_vec'] = torch.tensor(_tar_toked_id, dtype=torch.long)

            self.samples.append(sample)

    def __getitem__(self, i):
        return self.samples[i]['text_vec'], self.samples[i]['target_vec']

    def __len__(self):
        return len(self.samples)


def pad_tensor(tensors, sort=True, pad_token=0):
    rows = len(tensors)
    lengths = [len(i) for i in tensors]
    max_t = max(lengths)

    output = tensors[0].new(rows, max_t)
    output.fill_(pad_token)  # 0 is a pad token here

    for i, (tensor, length) in enumerate(zip(tensors, lengths)):
        output[i, :length] = tensor

    return output, lengths


def argsort(keys, *lists, descending=False):
    """Reorder each list in lists by the (descending) sorted order of keys.
    :param iter keys: Keys to order by.
    :param list[list] lists: Lists to reordered by keys's order.
                             Correctly handles lists and 1-D tensors.
    :param bool descending: Use descending order if true.
    :returns: The reordered items.
    """
    ind_sorted = sorted(range(len(keys)), key=lambda k: keys[k])
    if descending:
        ind_sorted = list(reversed(ind_sorted))
    output = []
    for lst in lists:
        if isinstance(lst, torch.Tensor):
            output.append(lst[ind_sorted])
        else:
            output.append([lst[i] for i in ind_sorted])
    return output


def batchify(batch):
    inputs = [i[0] for i in batch]
    labels = [i[1] for i in batch]

    input_vecs, input_lens = pad_tensor(inputs)
    label_vecs, label_lens = pad_tensor(labels)

    # sort only wrt inputs here for encoder packinng
    input_vecs, input_lens, label_vecs, label_lens = argsort(input_lens, input_vecs, input_lens, label_vecs, label_lens,
                                                             descending=True)

    return {
        "text_vecs": input_vecs,
        "text_lens": input_lens,
        "target_vecs": label_vecs,
        "target_lens": label_lens,
        'use_packed': True
    }


class _HypothesisTail(object):
    """Hold some bookkeeping about a hypothesis."""

    # use slots because we don't want dynamic attributes here
    __slots__ = ['timestep', 'hypid', 'score', 'tokenid']

    def __init__(self, timestep, hypid, score, tokenid):
        self.timestep = timestep
        self.hypid = hypid
        self.score = score
        self.tokenid = tokenid


def reorder_encoder_states(encoder_states, indices):
    """Reorder encoder states according to a new set of indices."""
    enc_out, hidden, attention_mask = encoder_states

    # LSTM or GRU/RNN hidden state?
    if isinstance(hidden, torch.Tensor):
        hid, cell = hidden, None
    else:
        hid, cell = hidden

    if not torch.is_tensor(indices):
        # cast indices to a tensor if needed
        indices = torch.LongTensor(indices).to(hid.device)

    hid = hid.index_select(1, indices)
    if cell is None:
        hidden = hid
    else:
        cell = cell.index_select(1, indices)
        hidden = (hid, cell)

    enc_out = enc_out.index_select(0, indices)
    attention_mask = attention_mask.index_select(0, indices)

    return enc_out, hidden, attention_mask


def reorder_decoder_incremental_state(incremental_state, inds):
    if torch.is_tensor(incremental_state):
        # gru or lstm
        return torch.index_select(incremental_state, 1, inds).contiguous()
    elif isinstance(incremental_state, tuple):
        return tuple(
            self.reorder_decoder_incremental_state(x, inds)
            for x in incremental_state)


def get_nbest_list_from_beam(beam, dictionary, n_best=None, add_length_penalty=False):
    if n_best is None:
        n_best = beam.min_n_best
    nbest_list = beam._get_rescored_finished(n_best=n_best, add_length_penalty=add_length_penalty)

    nbest_list_text = [(dictionary.v2t(i[0].cpu().tolist()), i[1].item()) for i in nbest_list]

    return nbest_list_text


