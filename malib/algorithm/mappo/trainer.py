# -*- coding: utf-8 -*-

import numpy as np
from collections import defaultdict

from malib.algorithm.common.trainer import Trainer
from malib.algorithm.mappo.data_generator import (
    recurrent_generator,
    simple_data_generator,
)
from malib.algorithm.mappo.loss import MAPPOLoss
from malib.utils.episode import EpisodeKey
import torch
import functools


def update_linear_schedule(optimizer, epoch, total_num_epochs, initial_lr):
    """Decreases the learning rate linearly"""
    lr = initial_lr - (initial_lr * (epoch / float(total_num_epochs)))
    for param_group in optimizer.param_groups:
        param_group["lr"] = lr


class MAPPOTrainer(Trainer):
    def __init__(self, tid):
        super(MAPPOTrainer, self).__init__(tid)
        self._loss = MAPPOLoss()

    def optimize(self, batch, **kwargs):
        total_opt_result = defaultdict(lambda: 0)
        _, _, n_agent, _ = batch[EpisodeKey.CUR_OBS].shape
        agent_dim = 2
        for k in batch:
            if batch[k].shape[agent_dim] != n_agent:
                batch[k] = np.repeat(batch[k], n_agent, axis=agent_dim)

        ppo_epoch = self.policy.custom_config["ppo_epoch"]
        num_mini_batch = self.policy.custom_config["num_mini_batch"]  # num_mini_batch
        num_updates = num_mini_batch * ppo_epoch

        if self._loss._policy.custom_config["use_rnn"]:
            data_generator_fn = functools.partial(
                recurrent_generator,
                batch,
                num_mini_batch,
                self._loss._policy.custom_config["rnn_data_chunk_length"],
                self._loss._policy.device,
            )
        else:
            len_traj, n_rollout_threads, n_agent, _ = batch[EpisodeKey.CUR_OBS].shape
            batch_size = len_traj * n_rollout_threads * n_agent
            for k in batch:
                batch[k] = torch.FloatTensor(batch[k].copy()).to(
                    self._loss._policy.device
                )
                batch[k] = batch[k].reshape([batch_size, -1])

            data_generator_fn = functools.partial(
                simple_data_generator, batch, num_mini_batch, self._loss._policy.device
            )

        for i_epoch in range(ppo_epoch):
            for mini_batch in data_generator_fn():
                tmp_opt_result = self.loss(mini_batch)
                for k, v in tmp_opt_result.items():
                    total_opt_result[k] += v / num_updates

        # TODO(ziyu & ming): find a way for customize optimizer and scheduler
        #  but now it doesn't affect the performance ...
        #
        # epoch = kwargs["epoch"]
        # total_epoch = kwargs["total_epoch"]
        # update_linear_schedule(
        #     self.loss.optimizers["critic"],
        #     epoch,
        #     total_epoch,
        #     self.loss._params["critic_lr"],
        # )
        # update_linear_schedule(
        #     self.loss.optimizers["actor"],
        #     epoch,
        #     total_epoch,
        #     self.loss._params["actor_lr"],
        # )
        return total_opt_result

    def preprocess(self, batch, **kwargs):
        pass