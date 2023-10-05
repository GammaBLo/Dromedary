import json
import os

import fire
import numpy as np
import torch
import transformers
from peft import PeftModel
from typing import List


assert (
    "LlamaTokenizer" in transformers._import_structure["models.llama"]
), "LLaMA is now in HuggingFace's main branch.\nPlease reinstall it: pip uninstall transformers && pip install git+https://github.com/huggingface/transformers.git"  # noqa: E501

from transformers import LlamaForCausalLM, AutoConfig

from peft import (  # noqa: E402
    LoraConfig,
    get_peft_model,
    set_peft_model_state_dict,
)


def translate_state_dict_key(k):  # noqa: C901
    k = k.replace("base_model.model.", "")
    if k == "model.embed_tokens.weight":
        return "tok_embeddings.weight"
    elif k == "model.norm.weight":
        return "norm.weight"
    elif k == "lm_head.weight":
        return "output.weight"
    elif k.startswith("model.layers."):
        layer = k.split(".")[2]
        if k.endswith(".self_attn.q_proj.weight"):
            return f"layers.{layer}.attention.wq.weight"
        elif k.endswith(".self_attn.k_proj.weight"):
            return f"layers.{layer}.attention.wk.weight"
        elif k.endswith(".self_attn.v_proj.weight"):
            return f"layers.{layer}.attention.wv.weight"
        elif k.endswith(".self_attn.o_proj.weight"):
            return f"layers.{layer}.attention.wo.weight"
        elif k.endswith(".mlp.gate_proj.weight"):
            return f"layers.{layer}.feed_forward.w1.weight"
        elif k.endswith(".mlp.down_proj.weight"):
            return f"layers.{layer}.feed_forward.w2.weight"
        elif k.endswith(".mlp.up_proj.weight"):
            return f"layers.{layer}.feed_forward.w3.weight"
        elif k.endswith(".input_layernorm.weight"):
            return f"layers.{layer}.attention_norm.weight"
        elif k.endswith(".post_attention_layernorm.weight"):
            return f"layers.{layer}.ffn_norm.weight"
        elif k.endswith("rotary_emb.inv_freq") or "lora" in k:
            return None
        else:
            print(layer, k)
            raise NotImplementedError
    else:
        print(k)
        raise NotImplementedError


def shard_weights(k, v, rank, total_ranks):
    def shard_dim(total_size):
        # shard size should be divisible by 64
        multiple_of = 8
        shard_size = total_size // total_ranks
        shard_size = multiple_of * ((shard_size + multiple_of - 1) // multiple_of)
        return shard_size

    if "tok_embeddings" in k or "wo" in k or "w2" in k:
        # split in the second demension
        total_dims = v.shape[1]
        shard_size = shard_dim(total_dims)
        start = rank * shard_size
        end = min((rank + 1) * shard_size, total_dims)
        return v[:, start:end].clone()

    elif "output" in k or "wq" in k or "wk" in k or "wv" in k or "w1" in k or "w3" in k:
        # split in the first demension
        total_dims = v.shape[0]
        shard_size = shard_dim(total_dims)
        start = rank * shard_size
        end = min((rank + 1) * shard_size, total_dims)
        return v[start:end, :].clone()

    elif "norm" in k or "rope" in k:
        # do not shard
        return v

    else:
        raise NotImplementedError


def main(
    base_model: str = "",
    lora_weights: str = "none",
    output_dir: str = None,
    total_ranks: int = 1,
    write_mode: bool = True,
):
    if output_dir is None:
        raise ValueError("output_dir must be specified")

    if lora_weights == "none":
        model = LlamaForCausalLM.from_pretrained(
            base_model,
            torch_dtype=torch.float16,
            device_map={"": "cpu"},
        )
        lora_model = model
    else:
        model = LlamaForCausalLM.from_pretrained(
            base_model,
            torch_dtype=torch.float16,
            device_map={"": "cpu"},
        )

        lora_model = PeftModel.from_pretrained(
            model,
            lora_weights,
            is_trainable=False,
        )

        lora_model = lora_model.merge_and_unload()

    lora_model.train(False)

    lora_model_sd = lora_model.state_dict()

    model_config = AutoConfig.from_pretrained(base_model)

    params = {
        "dim": model_config.hidden_size,
        "multiple_of": 256,
        "n_heads": model_config.num_attention_heads,
        "n_layers": model_config.num_hidden_layers,
        "norm_eps": model_config.rms_norm_eps,
        "vocab_size": -1,
    }

    if model_config.num_key_value_heads != model_config.num_attention_heads:
        params["n_kv_heads"] = model_config.num_key_value_heads

    if int(4 * 2 * model_config.hidden_size / 3) != model_config.intermediate_size:
        assert model_config.hidden_size == 8192
        assert model_config.intermediate_size == 28672
        params["ffn_dim_multiplier"] = 1.3
        params["multiple_of"] = 4096

    n_heads = params["n_heads"]
    real_n_kv_heads = model_config.num_key_value_heads
    dim = params["dim"]

    if real_n_kv_heads is not None:
        kv_multiplier = n_heads // real_n_kv_heads
    else:
        kv_multiplier = 1
        real_n_kv_heads = n_heads

    def unpermute(w, n_heads_in):
        return (
            w.view(n_heads_in, 2, dim // n_heads // 2, dim)
            .transpose(1, 2)
            .reshape(-1, dim)
        )

    os.makedirs(output_dir, exist_ok=False)
    print("Making output directory: ", output_dir)
    with open(os.path.join(output_dir, "params.json"), "w") as f:
        json.dump(params, f)

    for rank in range(total_ranks):
        new_state_dict = {}

        model_params_count = 0

        for k, v in lora_model_sd.items():
            new_k = translate_state_dict_key(k)
            if new_k is not None:
                if "wq" in new_k:
                    new_v = unpermute(v, n_heads)
                elif "wk" in new_k:
                    new_v = unpermute(v, real_n_kv_heads)
                else:
                    new_v = v

                new_v = shard_weights(new_k, new_v, rank, total_ranks)
                if "layers" not in new_k or "layers.0" in new_k:
                    print(f"{new_k},", "shape:", new_v.shape, "dtype:", new_v.dtype)

                v_np = new_v.cpu().numpy()
                new_v = torch.from_numpy(v_np.astype(np.float16))

                new_state_dict[new_k] = new_v
                model_params_count += new_v.numel()

        print(f"Total model params: {model_params_count}")

        print(
            f"Estimated storage: {model_params_count * 2 / 1024 / 1024 / 1024:.2f} GB"
        )
        if write_mode:
            print(
                f"Saving to: {os.path.join(output_dir, f'consolidated.{rank:02d}.pth')}"
            )
            torch.save(
                new_state_dict, os.path.join(output_dir, f"consolidated.{rank:02d}.pth")
            )
        else:
            print(
                f"Debug: saving to: {os.path.join(output_dir, f'consolidated.{rank:02d}.pth')}"
            )


if __name__ == "__main__":
    fire.Fire(main)
