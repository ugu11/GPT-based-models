import torch
from torch import nn
import numpy as np
import math
from torch.nn import functional as F

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

class FullyConnectedLayers(nn.Module):
    def __init__(self, base_size, dropout, bias=True):
        super().__init__()
        self.fc_input = nn.Sequential(
            nn.Linear(base_size, 4 * base_size, bias=bias),
            nn.ReLU()
        )
        self.fc_output = nn.Sequential(
            nn.Linear(4 * base_size, base_size, bias=bias),
            nn.ReLU()
        )
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        x = self.fc_input(x)
        x = self.fc_output(x)
        x = self.dropout(x)
        return x
    
class CausalSelfAttention(nn.Module):
    def __init__(self, n_embd, n_head, dropout, bias, block_size):
        super().__init__()
        assert n_embd % n_head == 0
        # key, query, value projections for all heads, but in a batch
        self.c_attn = nn.Linear(n_embd, 3 * n_embd, bias=bias)
        # output projection
        self.c_proj = nn.Linear(n_embd, n_embd, bias=bias)
        # regularization
        self.attn_dropout = nn.Dropout(dropout)
        self.resid_dropout = nn.Dropout(dropout)
        self.n_head = n_head
        self.n_embd = n_embd
        self.dropout = dropout
        # flash attention make GPU go brrrrr but support is only in PyTorch >= 2.0
        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention')
        if not self.flash:
            print("WARNING: using slow attention. Flash Attention requires PyTorch >= 2.0")
            # causal mask to ensure that attention is only applied to the left in the input sequence
            self.register_buffer("bias", torch.tril(torch.ones(block_size, block_size))
                                        .view(1, 1, block_size, block_size))

    def forward(self, x):
        B, T, C = x.size() # batch size, sequence length, embedding dimensionality (n_embd)

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        q, k, v  = self.c_attn(x).split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        if self.flash:
            # efficient attention using Flash Attention CUDA kernels
            y = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=None, dropout_p=self.dropout if self.training else 0, is_causal=True)
        else:
            # manual implementation of attention
            att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
            att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf'))
            att = F.softmax(att, dim=-1)
            att = self.attn_dropout(att)
            y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y

    
class TransformerBlock(nn.Module):
    def __init__(self, n_heads, embedding_dim, bias):
        super().__init__()
        self.n_heads = n_heads
        self.embedding_dim = embedding_dim
        self.head_dim = embedding_dim // n_heads
        self.attention_projection = nn.Linear(embedding_dim, 3 * embedding_dim, bias=bias)
        # self.multihead_attention = CausalSelfAttention(embedding_dim, n_heads)
        self.multihead_attention = CausalSelfAttention(embedding_dim, n_heads, 0.3, True, 1024)
        #  n_embd, n_head, dropout, bias, block_size
        self.layernorm_1 = nn.LayerNorm(embedding_dim)
        self.fc_layers = FullyConnectedLayers(embedding_dim, 0.3)
        self.layernorm_2 = nn.LayerNorm(embedding_dim)

    def forward(self, x):

        residual = x
        x = self.multihead_attention(x)
        x = self.layernorm_1(x + residual)
        residual  = x
        x = self.fc_layers(x)
        x = self.layernorm_2(x + residual)
        return x
    
class ClassificationHead(nn.Module):
    def __init__(self, embedding_dim, output_size):
        super().__init__()
        self.fc_layer = nn.Linear(embedding_dim, output_size)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, x):
        x = self.fc_layer(x)
        x = self.softmax(x)
        return x

class NanoGPTClassifier(nn.Module):
    def __init__(self, output_size, n_transformer_blocks, n_embeddings, embedding_dim):
        super().__init__()
        self.n_transformer_blocks = n_transformer_blocks
        self.n_embeddings = n_embeddings
        self.embedding_dim = embedding_dim
        self.output_size = output_size

        # Layers
        self.embedding = nn.Embedding(n_embeddings, embedding_dim)
        self.transformer_blocks = [TransformerBlock(10, embedding_dim, False) for _ in range(n_transformer_blocks)]
        self.output_head = ClassificationHead(embedding_dim, output_size)

    def forward(self, features):
        # Embedding
        X = self.embedding(features)

        # Transformer blocks
        for transformer_block in self.transformer_blocks:
            X = transformer_block(X)

        # Classifier layers
        X = self.output_head(X)
        
        return X

    def train(self, X, y, optimizer, loss_criterion, epochs=10, batch_size=64):
        losses = []
        loss = 0
        batch_progress = 0
        n_batches = np.round(len(X) / batch_size).astype(np.int)
        print(X.shape)
        
        X = torch.reshape(X, (n_batches, batch_size, X.shape[1])).to(device)
        # X = torch.from_numpy(X).to(device)

        y = torch.reshape(y, (n_batches, batch_size, y.shape[1])).to(device)
        # y = torch.from_numpy(y).to(device)
        
        print("Starting training...")
        for epoch in range(epochs):
            loss = 0
            print(f'Epoch {epoch}/{epochs} - ', end="")
            
            for i in range(n_batches):
                # Generate batch noisy images
                optimizer.zero_grad()
                
                # compute reconstructions
                outputs = self.forward(X[i])
                
                # compute training reconstruction loss
                train_loss = loss_criterion(outputs, y[i]).to(device)

                # compute accumulated gradients for generator and discriminator
                train_loss.backward()
                
                # perform parameter update based on current gradients only for the generator
                optimizer.step()

                # add the mini-batch training loss to epoch loss
                loss += train_loss.item()

                #progress += step_size
                batch_progress += 1
                print('#', end="")

            losses.append(loss)

            print(f', loss: {loss}')
            
        return losses