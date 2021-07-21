from typing import Optional, Tuple

import torch
import torch.nn as nn
from torch import Tensor
from transformers import GPT2Config, GPT2Model

import agent
from agent import Flatten, NNBase
from distributions import Bernoulli, Categorical, DiagGaussian
from utils import init


class Agent(agent.Agent):
    def __init__(self, obs_shape, action_space, **kwargs):
        nn.Module.__init__(self)

        self.base = Base(obs_shape[0], **kwargs)

        if action_space.__class__.__name__ == "Discrete":
            num_outputs = action_space.n
            self.dist = Categorical(self.base.output_size, num_outputs)
        elif action_space.__class__.__name__ == "Box":
            num_outputs = action_space.shape[0]
            self.dist = DiagGaussian(self.base.output_size, num_outputs)
        elif action_space.__class__.__name__ == "MultiBinary":
            num_outputs = action_space.shape[0]
            self.dist = Bernoulli(self.base.output_size, num_outputs)
        else:
            raise NotImplementedError


class GPTCell(nn.Module):
    def __init__(self, context_size, gpt: GPT2Model):
        super().__init__()
        self.gpt = gpt
        self.context_size = context_size

    def forward(
        self, input: Tensor, hx: Optional[Tensor] = None
    ) -> Tuple[Tensor, Tensor]:  # noqa: F811
        hx = hx.reshape(-1, self.context_size, self.gpt.config.n_embd)
        hx = torch.cat([hx, input.reshape(1, -1, 1)])
        hx = hx[:, 1:]
        output = self.gpt(inputs_embeds=hx)
        return output, hx


class Base(NNBase):
    def __init__(
        self,
        num_inputs,
        gpt_size: str,
        num_embeddings: int,
        randomize_parameters: bool,
        hidden_size=512,
        recurrent=False,
    ):
        super().__init__(recurrent, hidden_size, hidden_size)

        gpt_size = "" if gpt_size == "small" else f"-{gpt_size}"
        gpt_size = f"gpt2{gpt_size}"
        self.gpt = (
            GPT2Model(
                GPT2Config.from_pretrained(
                    gpt_size,
                    use_cache=False,
                    output_attentions=False,
                    output_hidden_states=False,
                )
            )
            if randomize_parameters
            else GPT2Model.from_pretrained(
                gpt_size,
                use_cache=False,
                output_attentions=False,
                output_hidden_states=False,
            )
        )
        self.rnn = GPTCell(gpt=self.gpt, context_size=num_embeddings)
        # Freeze GPT parameters
        for p in self.gpt.parameters():
            p.requires_grad_(False)
        embedding_size = self.gpt.config.n_embd

        init_ = lambda m: init(
            m,
            nn.init.orthogonal_,
            lambda x: nn.init.constant_(x, 0),
            nn.init.calculate_gain("relu"),
        )
        self.main = nn.Sequential(
            init_(nn.Conv2d(num_inputs, 32, 8, stride=4)),
            nn.ReLU(),
            init_(nn.Conv2d(32, 64, 4, stride=2)),
            nn.ReLU(),
            init_(nn.Conv2d(64, 32, 3, stride=1)),
            nn.ReLU(),
            Flatten(),
            init_(nn.Linear(32 * 7 * 7, num_embeddings * embedding_size)),
            nn.Unflatten(-1, (num_embeddings, embedding_size)),
        )
        self.gpt_output = nn.Sequential(
            init_(nn.Linear(embedding_size, hidden_size)),
            nn.ReLU(),
        )

        init_ = lambda m: init(
            m, nn.init.orthogonal_, lambda x: nn.init.constant_(x, 0)
        )
        self.critic_linear = init_(nn.Linear(hidden_size, 1))

        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        perception = self.main(inputs / 255.0)

        if self.is_recurrent:
            x, rnn_hxs = self._forward_gru(perception, rnn_hxs, masks)
        else:
            x = self.gpt(inputs_embeds=perception).last_hidden_state[:, -1]
            x = self.gpt_output(x)

        return self.critic_linear(x), x, rnn_hxs
