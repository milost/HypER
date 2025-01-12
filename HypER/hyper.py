from pathlib import Path

from tqdm import tqdm, trange

from load_data import Data
import numpy as np
import torch
from torch.nn import functional as F, Parameter
from torch.nn.init import xavier_normal_, xavier_uniform_
import time
from collections import defaultdict
from models import *
from torch.optim.lr_scheduler import ExponentialLR
import argparse
import pickle

    
class Experiment:

    def __init__(self, model_name, learning_rate=0.001, ent_vec_dim=200, rel_vec_dim=200, 
                 num_iterations=100, batch_size=128, decay_rate=0., cuda=False, 
                 input_dropout=0., hidden_dropout=0., feature_map_dropout=0.,
                 in_channels=1, out_channels=32, filt_h=3, filt_w=3, label_smoothing=0.):
        self.model_name = model_name
        self.learning_rate = learning_rate
        self.ent_vec_dim = ent_vec_dim
        self.rel_vec_dim = rel_vec_dim
        self.num_iterations = num_iterations
        self.batch_size = batch_size
        self.decay_rate = decay_rate
        self.label_smoothing = label_smoothing
        self.cuda = cuda
        self.kwargs = {"input_dropout": input_dropout, "hidden_dropout": hidden_dropout,
                       "feature_map_dropout": feature_map_dropout, "in_channels":in_channels,
                       "out_channels": out_channels, "filt_h": filt_h, "filt_w": filt_w}
        
    def get_data_idxs(self, data):
        data_idxs = [(self.entity_idxs[data[i][0]], self.relation_idxs[data[i][1]], \
                      self.entity_idxs[data[i][2]]) for i in range(len(data))]
        return data_idxs
    
    def get_er_vocab(self, data):
        er_vocab = defaultdict(list)
        for triple in data:
            er_vocab[(triple[0], triple[1])].append(triple[2])
        return er_vocab

    def get_batch(self, er_vocab, er_vocab_pairs, idx):
        batch = er_vocab_pairs[idx:min(idx+self.batch_size, len(er_vocab_pairs))]
        targets = np.zeros((len(batch), len(d.entities)))
        for idx, pair in enumerate(batch):
            targets[idx, er_vocab[pair]] = 1.
        targets = torch.FloatTensor(targets)
        if self.cuda:
            targets = targets.cuda()
        return np.array(batch), targets

    
    def evaluate(self, model, data):
        hits = []
        ranks = []
        for i in range(10):
            hits.append([])

        test_data_idxs = self.get_data_idxs(data)
        er_vocab = self.get_er_vocab(self.get_data_idxs(d.data))

        print("Number of data points: %d" % len(test_data_idxs))
        
        for i in range(0, len(test_data_idxs), self.batch_size):
            data_batch, _ = self.get_batch(er_vocab, test_data_idxs, i)
            e1_idx = torch.tensor(data_batch[:, 0])
            r_idx = torch.tensor(data_batch[:, 1])
            e2_idx = torch.tensor(data_batch[:, 2])
            if self.cuda:
                e1_idx = e1_idx.cuda()
                r_idx = r_idx.cuda()
                e2_idx = e2_idx.cuda()
            predictions = model.forward(e1_idx, r_idx)

            for j in range(data_batch.shape[0]):
                filt = er_vocab[(data_batch[j][0], data_batch[j][1])]
                target_value = predictions[j,e2_idx[j]].item()
                predictions[j, filt] = 0.0
                predictions[j, e2_idx[j]] = target_value

            sort_values, sort_idxs = torch.sort(predictions, dim=1, descending=True)

            sort_idxs = sort_idxs.cpu().numpy()
            for j in range(data_batch.shape[0]):
                rank = np.where(sort_idxs[j]==e2_idx.cpu().numpy()[j])[0][0]
                ranks.append(rank+1)

                for hits_level in range(10):
                    if rank <= hits_level:
                        hits[hits_level].append(1.0)
                    else:
                        hits[hits_level].append(0.0)

        print('Hits @10: {0}'.format(np.mean(hits[9])))
        print('Hits @3: {0}'.format(np.mean(hits[2])))
        print('Hits @1: {0}'.format(np.mean(hits[0])))
        print('Mean rank: {0}'.format(np.mean(ranks)))
        print('Mean reciprocal rank: {0}'.format(np.mean(1./np.array(ranks))))

    def save_as_numpy(self, dictionary, file):
        elements = []
        for key, value in dictionary.items():
            elements.append(key)
        elements = np.array(elements)
        np.save(file, elements)

    def train_and_eval(self, data_dir):
        print("Training the %s model..." % model_name)
        self.entity_idxs = {d.entities[i]:i for i in range(len(d.entities))}
        self.relation_idxs = {d.relations[i]:i for i in range(len(d.relations))}
        train_data_idxs = self.get_data_idxs(d.train_data)
        print("Number of training data points: %d" % len(train_data_idxs))

        # save the mapping of entities and relations to their corresponding index positions
        self.save_as_numpy(self.entity_idxs, data_dir+'/entities.npy')
        self.save_as_numpy(self.relation_idxs, data_dir+'/relations.npy')

        if model_name.lower() == "hype":
            model = HypE(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        elif model_name.lower() == "hyper":
            model = HypER(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        elif model_name.lower() == "distmult":
            model = DistMult(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        elif model_name.lower() == "conve":
            model = ConvE(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        elif model_name.lower() == "complex":
            model = ComplEx(d, self.ent_vec_dim, self.rel_vec_dim, **self.kwargs)
        print([value.numel() for value in model.parameters()])

        if self.cuda:
            model.cuda()
        model.init()
        opt = torch.optim.Adam(model.parameters(), lr=self.learning_rate)
        if self.decay_rate:
            scheduler = ExponentialLR(opt, self.decay_rate)

        er_vocab = self.get_er_vocab(train_data_idxs)
        er_vocab_pairs = list(er_vocab.keys())
        print(len(er_vocab_pairs))

        print("Starting training...")

        with trange(1, self.num_iterations+1) as t:
            for it in t:
                t.set_description(f'Epoch {it}')
                model.train()
                losses = []
                np.random.shuffle(er_vocab_pairs)
                with trange(0, len(er_vocab_pairs), self.batch_size) as t2:
                    for j in t2:
                        t2.set_description(f'Progress {j}')
                        data_batch, targets = self.get_batch(er_vocab, er_vocab_pairs, j)
                        opt.zero_grad()
                        e1_idx = torch.tensor(data_batch[:,0])
                        r_idx = torch.tensor(data_batch[:,1])
                        if self.cuda:
                            e1_idx = e1_idx.cuda()
                            r_idx = r_idx.cuda()
                        predictions = model.forward(e1_idx, r_idx)
                        if self.label_smoothing:
                            targets = ((1.0-self.label_smoothing)*targets) + (1.0/targets.size(1))
                        loss = model.loss(predictions, targets)
                        loss.backward()
                        opt.step()
                        t2.set_postfix(batch_loss=loss.item())
                if self.decay_rate:
                    scheduler.step()
                losses.append(loss.item())
                t.set_postfix(avg_loss=np.mean(losses))

                if d.valid_data and d.test_data:
                    model.eval()
                    with torch.no_grad():
                        print("Validation:")
                        self.evaluate(model, d.valid_data)
                        if not it%2:
                            print("Test:")
                            self.evaluate(model, d.test_data)

                # create model path if it doesn't exist
                model_save_path = Path(f'{data_dir}/model')
                model_save_path.mkdir(parents=True, exist_ok=True)

                # save entire torch model after each epoch
                torch.save(model.state_dict(),
                           str(model_save_path / Path(f'{self.model_name.lower()}_model.torch')))

                # create embedding path if it doesn't exist
                embeddings_save_path = Path(f'{data_dir}/embeddings')
                embeddings_save_path.mkdir(parents=True, exist_ok=True)

                # save entity and relation embeddings after each epoch
                torch.save(model.E.weight, str(embeddings_save_path / Path(f'{self.model_name.lower()}_entity_embeddings.torch')))
                torch.save(model.R.weight, str(embeddings_save_path / Path(f'{self.model_name.lower()}_relation_embeddings.torch')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--algorithm', type=str, default="HypER", nargs="?",
                    help='Which algorithm to use: HypER, ConvE, DistMult, or ComplEx')
    parser.add_argument('--dataset', type=str, default="FB15k-237", nargs="?",
                        help='Which dataset to use: FB15k, FB15k-237, WN18, WN18RR, or YAGO')
    parser.add_argument('--cuda', type=bool, default=False, nargs="?",
                        help='Enable CUDA')
    args = parser.parse_args()
    model_name = args.algorithm
    dataset = args.dataset
    data_dir = "data/%s/" % dataset
    torch.backends.cudnn.deterministic = True 
    seed = 42
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available:
        torch.cuda.manual_seed_all(seed)  
    d = Data(data_dir=data_dir, reverse=True)
    experiment = Experiment(model_name, num_iterations=800, batch_size=128, learning_rate=0.001, 
                            decay_rate=0.99, ent_vec_dim=200, rel_vec_dim=200, cuda=args.cuda,
                            input_dropout=0.2, hidden_dropout=0.3, feature_map_dropout=0.2,
                            in_channels=1, out_channels=32, filt_h=1, filt_w=9, label_smoothing=0.1)
    experiment.train_and_eval(data_dir)
