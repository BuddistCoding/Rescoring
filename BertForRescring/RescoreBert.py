import torch
import logging
from torch.nn.functional import log_softmax
from transformers import BertForMaskedLM, BertModel, BertTokenizer
from torch.optim import AdamW

class MLMBert(torch.nn.Module):
    def __init__(
        self, 
        train_batch, 
        test_batch, 
        nBest, 
        device, 
        lr = 1e-5, 
        mode = 'random'
        ):
        self.device = device
        self.model = BertForMaskedLM.from_pretrained("bert-base-chinese").to(device)
        self.tokenizer = BertTokenizer.from_pretrained("bert-base-chinese")
        self.mask = self.tokenizer.convert_tokens_to_ids("[MASK]")
        self.train_batch = train_batch
        self.test_batch = test_batch
        self.num_nBest = nBest
        self.mode = mode
        self.optimizer = AdamW(self.model.parameters(), lr = lr)

    def forward(self, input_id, segment, mask):
        # random mask reference : https://github.com/jamescalam/transformers/blob/main/course/training/03_mlm_training.ipynb
        
        if (self.mode == 'random'):
            label = input_id.clone().detach()
            rand = torch.rand(input_id.shape).to(self.device)
            mask_index = (rand < 0.15) * (input_id != 101) * (input_id != 102) * (input != 0)
            selection = []
            for i in range(input_id.shape[0]):
                selection.append(torch.flatten(mask_index[i].nonzero()).tolist())
            
            for i in range(input_id.shape[0]):
                input_id[i, selection[i]] = self.mask
            label[input_id != self.mask] = -100
            
            output = self.model(
                input_ids = input_id, 
                token_type_ids = segment,
                attention_mask = mask,
                labels = label
            )

            loss = output.loss

            return loss

        elif (self.mode == 'sequence'):
            batch_size = self.train_batch
            
            mlm_seq = []
            mlm_seg = []
            mlm_mask = []
            labels = []
            for i in range(batch_size):
                id_list = input_id[i].tolist()
                sep = id_list.index(102)
                mask_seq = input_id[i].clone()
                segs = segment[i].clone()
                att_mask = mask[i].clone()
                for j in range(1, sep):

                    selection = mask_seq[j].item()
                    mask_seq[j] = self.mask
                    mlm_seq.append(mask_seq.clone())
                    mlm_seg.append(segs.clone())
                    mlm_mask.append(att_mask)

                    label = mask_seq.clone()
                    label[label != self.mask] = -100
                    label[label == self.mask] = selection
                    labels.append(label)

                    mask_seq[j] = selection
            
            mlm_seq = torch.stack(mlm_seq).to(self.device)
            mlm_seg = torch.stack(mlm_seg).to(self.device)
            mlm_mask = torch.stack(mlm_mask).to(self.device)
            labels = torch.stack(labels).to(self.device)

            output = self.teacher(
                input_ids = mlm_seq ,
                token_type_ids = mlm_seg ,
                attention_mask = mlm_mask,
                labels = labels
            )

            loss = output.loss

            return loss

    def recognize(self, input_id, segment, attention_mask):
         # generate PLL loss from teacher
        pll_score = []  # (batch_size, N-Best)
        pll_input = []
        pll_seg = []
        pll_mask = []
        mask_index = []
        seq_for_hyp = []

        expand_num = 0

        no_token = set()
        for j in range(input_id.shape[0]): # for every hyp in this batch
            token_len = torch.sum(input_id[j] != 0, dim = -1)
            if (token_len == 2):
                no_token.add(j)
                continue

            seg = segment[j]
            mask = attention_mask[j]
            tmp = input_id[j].clone()
            for k in range(1 , token_len - 1): # for each token in this hyp (exclude padding)
                to_mask = tmp[k].item()
                tmp[k] = self.mask

                pll_input.append(tmp.clone())
                pll_seg.append(seg)
                pll_mask.append(mask)
                mask_index.append([expand_num, k, to_mask])
                expand_num += 1
                tmp[k] = to_mask

            seq_for_hyp.append(expand_num - 1)
        
        pll_input = torch.stack(pll_input).to(self.device)
        pll_seg = torch.stack(pll_seg).to(self.device)
        pll_mask = torch.stack(pll_mask).to(self.device)
        mask_index = torch.tensor(mask_index)
        

        outputs = self.model(
            input_ids = pll_input, 
            token_type_ids = pll_seg,
            attention_mask = pll_mask
        )
        outputs = log_softmax(outputs[0], dim = -1)
        mask_index = torch.transpose(mask_index, 0, 1)
        pll_score = outputs[mask_index[0], mask_index[1], mask_index[2]].tolist()
        
        pll = []
        count = 0
        accum_score = 0.0
        for i, score in enumerate(pll_score):
            accum_score += score
            if (i in seq_for_hyp):
                pll.append( -1 * accum_score)
                accum_score = 0.0
                count += 1
            if (count in no_token):
                pll.append(1e8)
                count += 1

        pll_score = torch.tensor(pll)

        return pll_score


class RescoreBert(torch.nn.Module):
    def __init__(
        self, 
        train_batch, 
        test_batch, 
        nBest,
        device, 
        weight = 1.0, 
        use_MWER = False, 
        use_MWED = False,
        lr = 1e-5
    ):
        torch.nn.Module.__init__(self)
        self.model= BertModel.from_pretrained("bert-base-chinese").to(device)
        self.tokenizer = BertTokenizer.from_pretrained("bert-base-chinese")
        self.train_batch = train_batch
        self.test_batch = test_batch
        self.nBest = nBest
        self.weight = weight
        self.use_MWER = use_MWER
        self.use_MWED = use_MWED
        self.device = device
        self.l2_loss = torch.nn.MSELoss()

        self.fc = torch.nn.Linear(768,1).to(device)
        self.optimizer = AdamW(self.model.parameters(), lr = lr)
        
    def forward(self, input_id, segment_id ,attention_mask, first_scores, cers, pll_score):
        """
        input_id : (B, N_Best, Seq)
        segment_id : (B, Nbest, Seq)
        """
        
        total_loss = 0.0
                
        loss_MWER = None
        loss_MWED = None
        
        s_output = self.model(input_ids = input_id, token_type_ids = segment_id,attention_mask = attention_mask)
        s_score = self.fc(s_output[0][:, 0])
            
        total_loss = self.l2_loss(pll_score, s_score.view(pll_score.shape))
        
        weight_sum = first_scores + self.weight * s_score
        
        # MWER
        if (self.use_MWER):
            wer = torch.stack(
                [
                    ((s[1] + s[2] + s[3]) / (s[0] + s[1] + s[2])) for s in cers
                ]
            ).to(self.device)
            p_hyp = torch.softmax(torch.neg(weight_sum), dim = -1)
            avg_error = torch.mean(wer)
            avg_error = torch.full(wer.shape, avg_error).to(self.device)
            
            loss_MWER = p_hyp * (wer - avg_error)

            loss_MWER = loss_MWER.sum()
            
            total_loss = loss_MWER + 1e-4 * total_loss
        
        # MWED
        elif (self.use_MWED):
            wer = torch.stack(
                [
                    ((s[1] + s[2] + s[3]) / (s[0] + s[1] + s[2])) for s in cers
                ]
            )
            d_error = torch.softmax(wer, dim = -1)
            d_score = torch.softmax(s_score, dim = -1)

            loss_MWED = d_error * torch.log(d_score)
            loss_MWED = -(loss_MWED.sum())
            
            total_loss = loss_MWED + 1e-4 * total_loss
        
        if (not self.training):
            weight_sum = weight_sum.view(self.test_batch, self.nBest)
            cers = cers.view(self.test_batch, self.nBest, -1)
            best_hyp = torch.argmax(weight_sum)

            return total_loss, cers[best_hyp]
        
        return total_loss

    def recognize(self, input_id, segment_id, attention_mask, first_scores, weight = 1.0):
        output = self.model(input_ids = input_id,token_type_ids = segment_id, attention_mask = attention_mask)
        rescore = self.fc(output[0][:, 0]).view(first_scores.shape)
        
        weighted_score = first_scores + (weight * rescore)
        weighted_score =  weighted_score.view(self.test_batch, self.nBest)

        max_sentence = torch.argmax(weighted_score, dim = -1)
        best_hyps = input_id[max_sentence].tolist()

        best_hyp_tokens = []
        for hyp in best_hyps:
            sep = hyp.index(102)
            best_hyp_tokens.append(self.tokenizer.convert_ids_to_tokens(hyp[1:sep]))

        return rescore, weighted_score, best_hyp_tokens, max_sentence
    
    