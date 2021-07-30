import pickle
import random
from pathlib import Path
from itertools import chain
from typing import Tuple

import pandas as pd
import numpy as np
import torch
import umap
from tap import Tap
from transformers import GPT2LMHeadModel, GPT2TokenizerFast

BASE_PATH = Path("/root/.cache/GPT/linguistic_analysis")
UMAP_CONFIGS = [
    ("5-neighbors-01-dist.pkl", dict(n_neighbors=5, min_dist=0.1)),
    ("5-neighbors-04-dist.pkl", dict(n_neighbors=5, min_dist=0.4)),
    ("20-neighbors-01-dist.pkl", dict(n_neighbors=20, min_dist=0.1)),
    ("20-neighbors-04-dist.pkl", dict(n_neighbors=20, min_dist=0.4)),
]


class Args(Tap):
    input_path: Path
    output_path: Path = BASE_PATH / "output"
    umap_path: Path = BASE_PATH / "UMAP"
    gpt_dists_path: Path = BASE_PATH / "gpt_dists.csv"
    gpt_size: str = "gpt-medium"
    seed: int = 1234
    n_random_embs: int = 5000
    top_k_neighbors: int = 5
    lm_batch_size: int = 8

    def configure(self):
        self.add_argument("input_path")
        self.add_argument("output_path")


def get_summary(df: pd.DataFrame, index_name: str) -> pd.DataFrame:
    result = df.describe().transpose()
    result.index.name = index_name
    result.reset_index(inplace=True)
    return result


def pairwise_cosine_distances(a, b, eps=1e-12):
    a_n = a.norm(dim=1)[:, None]
    b_n = b.norm(dim=1)[:, None]
    a_norm = a / torch.max(a_n, eps * torch.ones_like(a_n))
    b_norm = b / torch.max(b_n, eps * torch.ones_like(b_n))
    return 1.0 - torch.mm(a_norm, b_norm.transpose(0, 1))


def relative_quantile_summary(
    pairwise_dists: torch.Tensor, quantiles: torch.Tensor
) -> pd.DataFrame:
    q = torch.quantile(pairwise_dists, quantiles, dim=1)
    q = pd.DataFrame(q.cpu().numpy().T, columns=quantile_labels)
    return get_summary(q, "quantile")


def get_neighbors(
    pairwise_dists, top_k: int, batch_size: int, seq_len: int
) -> Tuple[np.ndarray, np.ndarray]:
    unflatten = torch.nn.Unflatten(0, (batch_size, seq_len))
    neighbor_dists, neighbors = torch.topk(pairwise_dists, top_k, dim=1)
    neighbor_dists = unflatten(neighbor_dists).cpu().numpy()
    neighbors = unflatten(neighbors).cpu().numpy()
    return (neighbors, neighbor_dists)


def model_lps(model: GPT2LMHeadModel, neighbors: torch.Tensor) -> torch.Tensor:
    # TODO: how to choose from the neighbors. should use same strategy as for perception.

    # For each position in the sequence, calculate the probability distribution of
    # the next token.
    logits = model(input_ids=batch).logits
    lps = torch.nn.functional.log_softmax(logits[:, :-1], dim=-1)

    # Extract the log-probability that was assigned to the token that actually appears
    # next.
    indices = batch[:, 1:, None]
    lps = lps.gather(-1, indices)

    # Calculate the log-probability of each sequence.
    return lps.sum(dim=1).squeeze()


def main(args: Args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    # Load input data.
    with args.input_path.open("rb") as f:
        analysis_data = torch.load(f)
    batch_size, seq_len, _ = analysis_data["perception"].shape
    observations = analysis_data["inputs"].detach().cpu().numpy().astype(np.uint8)
    actions = analysis_data["action_log_probs"].detach().cpu().numpy()
    perceptions = analysis_data["perception"]
    flat_perceptions = torch.flatten(perceptions, start_dim=0, end_dim=1)
    np_perceptions = flat_perceptions.cpu().numpy()
    del analysis_data

    # Load tokenizer, model, and embeddings.
    tokenizer = GPT2TokenizerFast.from_pretrained(args.gpt_size)
    token_texts = tokenizer.batch_decode([[i] for i in range(tokenizer.vocab_size)])
    model = GPT2LMHeadModel.from_pretrained(args.gpt_size)
    embs = model.get_input_embeddings().weight.detach().cuda()
    np_embs = embs.cpu().numpy()

    # Calculate UMAP coordinates
    umap_df = pd.DataFrame(columns=["x", "y", "source", "token", "umap"])
    for name, conf in UMAP_CONFIGS:
        # Load or create the UMAP model
        umap_path = args.umap_path / name
        if not umap_path.exists():
            umap_op = umap.UMAP(n_components=2, random_state=args.seed, **conf)
            umap_trf = umap_op.fit(np_embs)
            with umap_path.open("wb") as f:
                pickle.dump(umap_trf, f)
        else:
            with umap_path.open("rb") as f:
                umap_trf = pickle.load(f)
        # Project the perception data into the lower dimensional sapce.
        perception_umap = umap_trf.transform(np_perceptions)
        perception_labels = list(
            chain.from_iterable(
                ([f"Perception (Env {i})"] * seq_len) for i in range(batch_size)
            )
        )
        embs_umap = umap_trf.embedding_
        df = pd.DataFrame(
            dict(
                x=np.concatenate([embs_umap[:, 0], perception_umap[:, 0]], axis=0),
                y=np.concatenate([embs_umap[:, 1], perception_umap[:, 1]], axis=0),
                source=["GPT"] * len(embs_umap) + perception_labels,
                token=token_texts + perception_labels,
                umap=name,
            )
        )
        umap_df.append(df)
    umap_df.to_csv(args.output_path / "umap-coordinates.csv", index=False)

    # Pairwise distances
    quantiles = torch.linspace(0, 1, 11).cuda()
    quantile_labels = quantiles.cpu().numpy().round(2).tolist()
    if not args.gpt_dists_path.exists():
        # Calculate pairwise distances between GPT rand_dists.
        # Divide by the norm to make computing the cosine distances easier
        eps = 1e-12  # Epsilon for stability
        embs_n = embs.norm(dim=1)[:, None]
        embs = embs / torch.max(embs_n, eps * torch.ones_like(embs_n))

        n = embs.shape[0]
        summaries = []
        for i in range(n):
            # Compute the cosine distance between the i-th embedding and all other embeddings.
            d = 1.0 - torch.mm(embs[i, :][None, :], embs.transpose(0, 1)).squeeze()
            # Remove the ith entry (distance-to-self)
            d = torch.cat([d[:i], d[i + 1 :]])
            summaries.append(torch.quantile(d, quantiles))
        summaries = torch.stack(summaries, dim=0)

        # Summarise the distances.
        gpt_summary = pd.DataFrame(summaries.cpu().numpy(), columns=quantile_labels)
        del summaries
        gpt_summary = get_summary(gpt_summary, "quantile")
        gpt_summary["embedding"] = "GPT-2"

        # Calculate the pairwise distances between GPT embeddings and random vectors.
        rand_pairwise = pairwise_cosine_distances(
            torch.rand((args.n_random_embs, model.config.n_embd)).cuda(), embs
        )
        rand_summary = relative_quantile_summary(rand_pairwise, quantiles)
        del rand_pairwise
        rand_summary["embedding"] = "Random"

        # Save to disk.
        gpt_summary = gpt_summary.append(rand_summary)
        gpt_summary.to_csv(args.gpt_dists_path, index=False)
    else:
        gpt_summary = pd.read_csv(args.gpt_dists_path)

    perception_pairwise = pairwise_cosine_distances(flat_perceptions, embs)
    perception_summary = relative_quantile_summary(perception_pairwise, quantiles)
    perception_summary["embedding"] = "Perception"
    perception_summary = perception_summary.append(gpt_summary)
    perception_summary.to_csv(args.output_path / "pairwise-distance-summary.csv")

    # For each perception embedding, find the top-k nearest GPT embeddings.
    neighbors, neighbor_dists = get_neighbors(
        perception_pairwise, args.top_k_neighbors, batch_size, seq_len
    )

    ############# TODO: Does it make sense to aggregate twice here? Maybe don't need quantiles.
    lm_lp_stats = model_lps(model, neighbors).quantile(quantiles).detach().cpu().numpy()
    lm_lp_stats = pd.DataFrame(lm_lp_stats, columns=quantile_labels)
    lm_lp_stats["embedding"] = "Perception"
    ########################################

    # Calculate the log-prob of random sequences of vectors under the model.
    n_batches = np.ciel(args.n_random_embs / (args.lm_batch_size * seq_len))
    sequence_lps = []
    for _ in range(n_batches):
        # Generate random sequences of embeddings and find their nearest neighbors.
        rand_embs = torch.rand(
            (args.lm_batch_size * seq_len, model.config.n_embd),
        ).cuda()
        rand_pairwise = pairwise_cosine_distances(rand_embs, embs)
        del rand_embs
        neighbors, _ = get_neighbors(
            rand_pairwise, args.top_k_neighbors, args.lm_batch_size, seq_len
        )
        del rand_pairwise
        # Calculate the log-probability of each sequence.
        sequence_lps.append(model_lps(model, neighbors))
    rand_stats = (
        pd.DataFrame(dict(random=torch.cat(sequence_lps).detach().cpu().numpy()))
        .describe()
        .transpose()
    )
    rand_stats["embedding"] = "Random"
    lm_lp_stats = lm_lp_stats.append(rand_stats)


if __name__ == "__main__":
    args = Args().parse_args()
    main(args)
