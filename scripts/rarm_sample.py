#!/usr/bin/env python
# coding: utf-8

import argparse
import datetime
import os
from pathlib import Path
from typing import Union

import numpy as np
import torch
import torchvision
from clip import tokenize
from einops import rearrange
from omegaconf import OmegaConf
from PIL import Image
from pytorch_lightning import seed_everything
from torchvision.utils import save_image as tvsave
from tqdm.auto import tqdm, trange

from ldm.util import instantiate_from_config
from rdm.models.autoregression.transformer import LatentImageRETRO


def load_model(opt: argparse.Namespace) -> LatentImageRETRO:
    model_dir = opt.model_path
    config_path = model_dir / "config.yaml"
    ckpt_path = model_dir / "model.ckpt"
    assert config_path.is_file(), f"Did not found config at {config_path}"
    assert ckpt_path.is_file(), f"Did not found ckpt at {ckpt_path}"
    # actually loading the model

    # Load model configuration and change some settings
    config = OmegaConf.load(config_path)
    config.model.params.retrieval_cfg.params.load_patch_dataset = opt.save_nns
    # Don't load anything on any gpu until told to do so
    # alternatively call with `CUDA_VISIBLE_DEVICES=...`
    config.model.params.retrieval_cfg.params.gpu = False
    config.model.params.retrieval_cfg.params.retriever_config.params.device = "cpu"

    # Load state dict
    pl_sd = torch.load(ckpt_path, map_location="cpu")

    # Initialize model
    model = instantiate_from_config(config.model)
    assert isinstance(model, LatentImageRETRO), "This scripts needs an object of type LatentImageRETRO"

    # Apply checkpoint
    m, u = model.load_state_dict(pl_sd["state_dict"])
    if len(m) > 0:
        print(f"Missing keys: \n {m}")
    if len(u) > 0:
        print(f"Unexpected keys: \n {u}")
    print("Loaded model.")

    # Eval mode
    model = model.eval()

    if opt.gpu >= 0:
        device = torch.device(f"cuda:{opt.gpu}")
        model = model.to(device)
        # retriever is no nn.Module, so device changes are not passed through
        model.retriever.retriever.to(device)

    return model


def rescale(x: torch.Tensor) -> torch.Tensor:
    return (x + 1.)/2.


def bchw_to_np(x, grid=False, clamp=False):
    if grid:
        x = torchvision.utils.make_grid(x, nrow=min(x.shape[0], 4))[None, ...]
    x = rescale(rearrange(x.detach().cpu(), "b c h w -> b h w c"))
    if clamp:
        x.clamp_(0, 1)
    return x.numpy()


def custom_to_pil(x: Union[np.ndarray, torch.Tensor]) -> Image.Image:
    if isinstance(x, np.ndarray):
        x = torch.from_numpy(x)
    x = x.detach().cpu()
    x = torch.clamp(x, -1., 1.)
    x = (x + 1.) / 2.
    x = x.permute(1, 2, 0).numpy()
    x = (255 * x).astype(np.uint8)
    x = Image.fromarray(x)
    if not x.mode == "RGB":
        x = x.convert("RGB")
    return x


def save_image(x, savename: str):
    img = custom_to_pil(x)
    img.save(savename)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-s",
        "--savepath",
        type=Path,
        default="out/rarm",
        help="Path to savedir",
    )
    parser.add_argument(
        "--gpu",
        type=int,
        default=-1,
        help="On which gpu to sample, -1 for none",
    )
    parser.add_argument(
        "--model_path",
        type=Path,
        default="models/rarm/imagenet/dogs",
        help="Path to pretrained model",
    )
    parser.add_argument(
        "--save_nns",
        default=False,
        action="store_true",
        help="Save nearest neighbors",
    )
    parser.add_argument(
        "-bs",
        "--batch_size",
        type=int,
        default=4,
        help="How many images to generate at once",
    )
    parser.add_argument(
        "-n",
        "--n_runs",
        type=int,
        default=2,
        help="repeat sampling this number of times",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed each iteration",
    )
    parser.add_argument(
        "--increase_guidance",
        default=False,
        action="store_true",
        help="Increase cfg after each iteration",
    )
    parser.add_argument(
        "--keep_qids",
        default=False,
        action="store_true",
        help="Keep same queries for each run",
    )
    parser.add_argument(
        "--guidance_scale",
        type=float,
        default=1.,
        help="classifier free (transformer) guidance",
    )
    parser.add_argument(
        "--top_k",
        type=int,
        default=256,
        help="top-k sampling",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=1.,
        help="temperature sampling",
    )
    parser.add_argument(
        "--top_m",
        type=float,
        default=0.01,
        help="top-m sampling",
    )
    parser.add_argument(
        "--k_nn",
        type=int,
        default=4,
        help="number of neighbors drawn for sampling",
    )
    parser.add_argument(
        "-c",
        "--caption",
        type=str,
        default="",
        help="Caption used for neighbor retrieval",
    )
    parser.add_argument(
        "--only_caption",
        default=False,
        action="store_true",
        help="use the caption only, no neighbors",
    )
    parser.add_argument(
        "--unconditional",
        default=False,
        action="store_true",
        help="Sample 'unconditonal' as in the unconditional part of cfg",
    )
    parser.add_argument(
        "--use_weights",
        default=False,
        action="store_true",
        help="Use proposal distribution weights (else sample uniform under top_m)",
    )
    opt = parser.parse_args()

    if opt.top_m > 1.0:
        # top_m should be int if a fixed number of images is given
        opt.top_m = int(opt.top_m)
    if opt.seed is not None and (not opt.increase_guidance) and opt.r_runs > 1:
        print("Warning: You will get the same images each run")
    return opt



def sample(model: LatentImageRETRO, opt: argparse.Namespace):
    with torch.no_grad():
        qids = None

        sampling_start = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        query_embeddings = None
        nn_embeddings = None
        if opt.caption != "":
            tokenized = tokenize([opt.caption]*opt.batch_size).to(model.device)
            clip = model.retriever.retriever.model
            query_embeddings = clip.encode_text(tokenized).cpu()
            del tokenized
        if opt.only_caption:
            assert opt.caption != "", "Need a caption"
            nn_embeddings = query_embeddings.unsqueeze(1).to(model.device).float()
        elif opt.unconditional:
            nn_embeddings = torch.zeros((opt.batch_size, 1, 512), dtype=torch.float, device=model.device)

        for n in trange(opt.n_runs):
            if opt.seed is not None:
                seed_everything(opt.seed)

            tqdm.write("Sampling query and neighbors (wait for the sampling to start)")
            S = 256 # number of steps (assumes first stage has f16)
            pbar = tqdm(total=S)
            progress_cb = lambda k: pbar.update()

            # Here is where the magic happens:
            logs = model.sample_from_rdata(
                    opt.batch_size,
                    qids=qids,
                    query_embeddings=query_embeddings,
                    nn_embeddings=nn_embeddings,
                    k_nn=opt.k_nn,
                    return_nns=opt.save_nns,
                    use_weights=opt.use_weights,
                    memsize=opt.top_m,
                    top_k=opt.top_k,
                    temperature=opt.temperature,
                    guidance_scale=opt.guidance_scale,
                    callback=progress_cb
            )
            if opt.keep_qids:
                assert "qids" in logs
                qids = logs["qids"]
            plotting_keys = []
            for key in logs:
                if (key == "samples_with_sampled_nns"
                        or (key == "sampled_nns" and (n==0 or not opt.keep_qids))
                        ):
                    plotting_keys.append(key)
            plotting_keys.sort()

            tqdm.write(f"Run {n+1}/{opt.n_runs}")
            for key in logs:
                if key in ["samples_with_sampled_nns", "batched_nns"]:
                    for bi,be in enumerate(logs[key]):
                        savename = os.path.join(opt.savepath,f'{sampling_start}-{key}-run{n}-sample{bi}.png')
                        if be.ndim == 3:
                            save_image(be,savename)
                        elif be.ndim == 4:
                            be = be.detach().cpu()
                            tvsave(be,savename, normalize=True, nrow=2)

            if opt.increase_guidance:
                opt.guidance_scale += 1.0
                tqdm.write(f"New guidance scale: {opt.guidance_scale}")

        print("Done")


if __name__ == "__main__":
    opt = parse_args()
    opt.savepath.mkdir(parents=True, exist_ok=True)
    model = load_model(opt)
    sample(model, opt)