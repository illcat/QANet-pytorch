import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from config import config

D = config.connector_dim
Nh = config.num_heads
Dword = config.glove_dim
Dchar = config.char_dim
dropout = config.dropout
dropout_char = config.dropout_char

Dk = D // Nh
Dv = D // Nh
D_cq_att = D * 4
sqrt_dk_inv = 1 / math.sqrt(Dk)
Lc = config.para_limit
Lq = config.ques_limit


def mask_logits(target, mask):
    return target * mask + (1 - mask) * (-1e30)


class PosEncoder(nn.Module):
    def __init__(self, length):
        super().__init__()
        self.positional_embedding = nn.Embedding(length, D)
        self.register_buffer('pos', torch.arange(0, length))

    def forward(self, x):
        size = x.size()
        positions = self.pos[:size[2]].unsqueeze(0).repeat(size[0], 1)
        x = x + self.positional_embedding(positions).transpose(1, 2)
        return x


class DepthwiseSeparableConv(nn.Module):
    def __init__(self, in_ch, out_ch, k, dim=1, bias=True):
        super().__init__()
        if dim == 1:
            self.depthwise_conv = nn.Conv1d(in_channels=in_ch, out_channels=in_ch, kernel_size=k, groups=in_ch,
                                            padding=k // 2, bias=bias)
            self.pointwise_conv = nn.Conv1d(in_channels=in_ch, out_channels=out_ch, kernel_size=1, padding=0, bias=bias)
        elif dim == 2:
            self.depthwise_conv = nn.Conv2d(in_channels=in_ch, out_channels=in_ch, kernel_size=k, groups=in_ch,
                                            padding=k // 2, bias=bias)
            self.pointwise_conv = nn.Conv2d(in_channels=in_ch, out_channels=out_ch, kernel_size=1, padding=0, bias=bias)
        else:
            raise Exception("Wrong dimension for Depthwise Separable Convolution!")
        nn.init.kaiming_normal_(self.depthwise_conv.weight)
        nn.init.constant_(self.depthwise_conv.bias, 0.0)
        nn.init.kaiming_normal_(self.depthwise_conv.weight)
        nn.init.constant_(self.pointwise_conv.bias, 0.0)

    def forward(self, x):
        return self.pointwise_conv(self.depthwise_conv(x))


class Highway(nn.Module):
    def __init__(self, layer_num: int, size: int):
        super().__init__()
        self.n = layer_num
        self.linear = nn.ModuleList([nn.Linear(size, size) for _ in range(self.n)])
        self.gate = nn.ModuleList([nn.Linear(size, size) for _ in range(self.n)])

    def forward(self, x):
        x = x.transpose(1, 2)
        for i in range(self.n):
            gate = torch.sigmoid(self.gate[i](x))
            nonlinear = F.relu(self.linear[i](x))
            x = gate * nonlinear + (1 - gate) * x
        return x.transpose(1, 2)


class SelfAttention(nn.Module):
    def __init__(self):
        super().__init__()
        self.Wqs = nn.Linear(D, D, bias=False)
        self.Wks = nn.Linear(D, D, bias=False)
        self.Wvs = nn.Linear(D, D, bias=False)
        self.Wo = nn.Linear(D, D, bias=False)

        nn.init.xavier_uniform_(self.Wqs.weight)
        nn.init.xavier_uniform_(self.Wks.weight)
        nn.init.xavier_uniform_(self.Wvs.weight)
        nn.init.xavier_uniform_(self.Wo.weight)

    def forward(self, x, mask):
        x = x.transpose(1, 2)
        size = x.size()
        WQs = self.Wqs(x).reshape(size[0], size[1], Nh, Dk).transpose(1, 2)
        WKs = self.Wqs(x).reshape(size[0], size[1], Nh, Dk).transpose(1, 2)
        WVs = self.Wqs(x).reshape(size[0], size[1], Nh, Dv).transpose(1, 2)

        Qk = sqrt_dk_inv * torch.matmul(WQs, WKs.transpose(2, 3))

        hmask = mask.unsqueeze(1).repeat(1, Nh, 1).unsqueeze(2)
        Qkm = F.softmax(mask_logits(Qk, hmask) * hmask.transpose(2, 3), dim=-1)
        
        Qkv = torch.matmul(Qkm, WVs).transpose(1, 2).reshape(size[0], size[1], D)
        out = self.Wo(Qkv)
        return out.transpose(1, 2)


class Embedding(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv2d = DepthwiseSeparableConv(Dchar, Dchar, 5, dim=2)
        self.high = Highway(2, Dword+Dchar)

    def forward(self, ch_emb, wd_emb):
        ch_emb = ch_emb.permute(0, 3, 1, 2)
        ch_emb = F.dropout(ch_emb, p=dropout_char, training=self.training)
        ch_emb = self.conv2d(ch_emb)
        ch_emb = F.relu(ch_emb)
        ch_emb, _ = torch.max(ch_emb, dim=3)
        ch_emb = ch_emb.squeeze()
        wd_emb = F.dropout(wd_emb, p=dropout, training=self.training)
        wd_emb = wd_emb.transpose(1, 2)
        emb = torch.cat([ch_emb, wd_emb], dim=1)
        emb = self.high(emb)
        return emb


class EncoderBlock(nn.Module):
    def __init__(self, conv_num: int, ch_num: int, k: int, length: int):
        super().__init__()
        self.convs = nn.ModuleList([DepthwiseSeparableConv(ch_num, ch_num, k) for _ in range(conv_num)])
        self.self_att = SelfAttention()
        self.fc = nn.Linear(ch_num, ch_num, bias=True)
        self.pos = PosEncoder(length)
        self.normb = nn.LayerNorm([D, length])
        self.norms = nn.ModuleList([nn.LayerNorm([D, length]) for _ in range(conv_num)])
        self.norme = nn.LayerNorm([D, length])
        self.L = conv_num

    def forward(self, x, mask):
        out = self.pos(x)
        res = out
        out = self.normb(out)
        for i, conv in enumerate(self.convs):
            out = conv(out)
            out = F.relu(out)
            out = out + res
            if (i + 1) % 2 == 0:
                p_drop = dropout * (i + 1) / self.L
                out = F.dropout(out, p=p_drop, training=self.training)
            res = out
            out = self.norms[i](out)
        out = self.self_att(out, mask)
        out = out + res
        out = F.dropout(out, p=dropout, training=self.training)
        res = out
        out = self.norme(out)
        out = self.fc(out.transpose(1, 2)).transpose(1, 2)
        out = F.relu(out)
        out = out + res
        out = F.dropout(out, p=dropout, training=self.training)
        return out


class CQAttention(nn.Module):
    def __init__(self):
        super().__init__()
        w = torch.empty(D * 3)
        lim = 1 / D
        nn.init.uniform_(w, -math.sqrt(lim), math.sqrt(lim))
        self.w = nn.Parameter(w)

    def forward(self, C, Q, cmask, qmask):
        ss = []
        C = C.transpose(1, 2)
        Q = Q.transpose(1, 2)
        cmask = cmask.unsqueeze(2)
        qmask = qmask.unsqueeze(1)
        
        shape = (C.size(0), C.size(1), Q.size(1), C.size(2))
        Ct = C.unsqueeze(2).expand(shape)
        Qt = Q.unsqueeze(1).expand(shape)
        CQ = torch.mul(Ct, Qt)
        S = torch.cat([Ct, Qt, CQ], dim=3)
        S = torch.matmul(S, self.w)
        S1 = F.softmax(mask_logits(S, qmask), dim=2)
        S2 = F.softmax(mask_logits(S, cmask), dim=1)
        A = torch.bmm(S1, Q)
        B = torch.bmm(torch.bmm(S1, S2.transpose(1, 2)), C)
        out = torch.cat([C, A, torch.mul(C, A), torch.mul(C, B)], dim=2)
        out = F.dropout(out, p=dropout, training=self.training)
        return out.transpose(1, 2)


class Pointer(nn.Module):
    def __init__(self):
        super().__init__()
        w1 = torch.empty(D * 2)
        w2 = torch.empty(D * 2)
        lim = 3 / (2 * D)
        nn.init.uniform_(w1, -math.sqrt(lim), math.sqrt(lim))
        nn.init.uniform_(w2, -math.sqrt(lim), math.sqrt(lim))
        self.w1 = nn.Parameter(w1)
        self.w2 = nn.Parameter(w2)

    def forward(self, M1, M2, M3, mask):
        X1 = torch.cat([M1, M2], dim=1)
        X2 = torch.cat([M1, M3], dim=1)
        Y1 = torch.matmul(self.w1, X1)
        Y2 = torch.matmul(self.w2, X2)
        Y1 = mask_logits(Y1, mask)
        Y2 = mask_logits(Y2, mask)
        p1 = F.log_softmax(Y1, dim=1)
        p2 = F.log_softmax(Y2, dim=1)
        return p1, p2


class QANet(nn.Module):
    def __init__(self, word_mat, char_mat):
        super().__init__()
        self.char_emb = nn.Embedding.from_pretrained(torch.Tensor(char_mat), freeze=config.pretrained_char)
        self.word_emb = nn.Embedding.from_pretrained(torch.Tensor(word_mat))
        self.emb = Embedding()
        self.context_conv = DepthwiseSeparableConv(Dword+Dchar,D, 5)
        self.question_conv = DepthwiseSeparableConv(Dword+Dchar,D, 5)
        self.c_emb_enc = EncoderBlock(conv_num=4, ch_num=D, k=7, length=Lc)
        self.q_emb_enc = EncoderBlock(conv_num=4, ch_num=D, k=7, length=Lq)
        self.cq_att = CQAttention()
        self.cq_resizer = DepthwiseSeparableConv(D * 4, D, 5)
        enc_blk = EncoderBlock(conv_num=2, ch_num=D, k=5, length=Lc)
        self.model_enc_blks = nn.ModuleList([enc_blk] * 7)
        self.out = Pointer()

    def forward(self, Cwid, Ccid, Qwid, Qcid):
        cmask = (Cwid != 0).float()
        qmask = (Qwid != 0).float()
        Cw, Cc = self.word_emb(Cwid), self.char_emb(Ccid)
        Qw, Qc = self.word_emb(Qwid), self.char_emb(Qcid)
        C, Q = self.emb(Cc, Cw), self.emb(Qc, Qw)
        C = self.context_conv(C)  
        Q = self.question_conv(Q)  
        Ce = self.c_emb_enc(C, cmask)
        Qe = self.q_emb_enc(Q, qmask)
        
        X = self.cq_att(Ce, Qe, cmask, qmask)
        M1 = self.cq_resizer(X)
        for enc in self.model_enc_blks: M1 = enc(M1, cmask)
        M2 = M1
        for enc in self.model_enc_blks: M2 = enc(M2, cmask)
        M3 = M2
        for enc in self.model_enc_blks: M3 = enc(M3, cmask)
        p1, p2 = self.out(M1, M2, M3, cmask)
        return p1, p2
