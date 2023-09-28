#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2022-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse as _arg
import os as _os
import pathlib as _pl
import subprocess as _sp
import typing as _tp

import hf_gpt_convert as _egc
import torch.multiprocessing as _mp

import build as _egb  # isort:skip


def build_engine(weigth_dir: _pl.Path, engine_dir: _pl.Path, world_size, *args):
    args = [
        '--log_level=error',
        '--model_dir',
        str(weigth_dir),
        '--output_dir',
        str(engine_dir),
        '--max_batch_size=256',
        '--max_input_len=40',
        '--max_output_len=20',
        '--max_beam_width=2',
        '--builder_opt=0',
        f'--world_size={world_size}',
    ] + list(args)
    print("Runnning: " + " ".join(args))
    _egb.run_build(args)


def run_command(command: _tp.Sequence[str], *, cwd=None, **kwargs) -> None:
    print(f"Running: cd %s && %s" %
          (str(cwd or _pl.Path.cwd()), " ".join(command)))
    _sp.check_call(command, cwd=cwd, **kwargs)


def build_engines(model_cache: _tp.Optional[str] = None, world_size: int = 1):
    resources_dir = _pl.Path(__file__).parent.resolve().parent
    models_dir = resources_dir / 'models'
    model_name = 'gpt2'

    # Clone or update the model directory without lfs
    hf_dir = models_dir / model_name
    if hf_dir.exists():
        assert hf_dir.is_dir()
        run_command(["git", "pull"], cwd=hf_dir)
    else:
        model_url = "file://" + str(
            _pl.Path(model_cache) /
            model_name) if model_cache else "https://huggingface.co/gpt2"
        run_command(["git", "clone", model_url, "--single-branch", model_name],
                    cwd=hf_dir.parent,
                    env={
                        **_os.environ, "GIT_LFS_SKIP_SMUDGE": "1"
                    })

    assert hf_dir.is_dir()

    # Download the model file
    model_file_name = "pytorch_model.bin"
    if model_cache:
        run_command([
            "rsync", "-av",
            str(_pl.Path(model_cache) / model_name / model_file_name), "."
        ],
                    cwd=hf_dir)
    else:
        run_command(["git", "lfs", "pull", "--include", model_file_name],
                    cwd=hf_dir)

    (hf_dir / "model.safetensors").unlink(missing_ok=True)

    assert (hf_dir / model_file_name).is_file()

    weight_dir = models_dir / 'c-model' / model_name
    engine_dir = models_dir / 'rt_engine' / model_name

    print("\nConverting to fp32")
    tp_dir = f"{world_size}-gpu"
    fp32_weight_dir = weight_dir / 'fp32'
    _egc.run_conversion(
        _egc.ProgArgs(in_file=str(hf_dir),
                      out_dir=str(fp32_weight_dir),
                      storage_type='float32',
                      tensor_parallelism=world_size))

    print("\nBuilding fp32 engines")
    fp32_weight_dir_x_gpu = fp32_weight_dir / tp_dir
    build_engine(fp32_weight_dir_x_gpu, engine_dir / 'fp32-default' / tp_dir,
                 world_size, '--dtype=float32')
    build_engine(fp32_weight_dir_x_gpu, engine_dir / 'fp32-plugin' / tp_dir,
                 world_size, '--dtype=float32',
                 '--use_gpt_attention_plugin=float32')

    print("\nConverting to fp16")
    fp16_weight_dir = weight_dir / 'fp16'
    _egc.run_conversion(
        _egc.ProgArgs(in_file=str(hf_dir),
                      out_dir=str(fp16_weight_dir),
                      storage_type='float16',
                      tensor_parallelism=world_size))

    print("\nBuilding fp16 engines")
    fp16_weight_dir_x_gpu = fp16_weight_dir / tp_dir
    build_engine(fp16_weight_dir_x_gpu, engine_dir / 'fp16-default' / tp_dir,
                 world_size, '--dtype=float16')
    build_engine(fp16_weight_dir_x_gpu, engine_dir / 'fp16-plugin' / tp_dir,
                 world_size, '--dtype=float16',
                 '--use_gpt_attention_plugin=float16')
    build_engine(fp16_weight_dir_x_gpu,
                 engine_dir / 'fp16-plugin-packed' / tp_dir, world_size,
                 '--dtype=float16', '--use_gpt_attention_plugin=float16',
                 '--remove_input_padding')
    build_engine(fp16_weight_dir_x_gpu,
                 engine_dir / 'fp16-plugin-packed-paged' / tp_dir, world_size,
                 '--dtype=float16', '--use_gpt_attention_plugin=float16',
                 '--remove_input_padding', '--paged_kv_cache')

    print("Done.")


if __name__ == "__main__":
    parser = _arg.ArgumentParser()
    parser.add_argument("--model_cache",
                        type=str,
                        help="Directory where models are stored")

    parser.add_argument('--world_size',
                        type=int,
                        default=1,
                        help='world size, only support tensor parallelism now')

    _mp.set_start_method("spawn")

    build_engines(**vars(parser.parse_args()))