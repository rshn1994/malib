import pytest
import numpy as np

from malib.utils.episode import Episode, NewEpisodeDict
from malib.rl.random import RandomPolicy
from malib.rollout.envs.dummy_env import DummyEnv
from malib.utils.tianshou_batch import Batch


def generate_a_transition(batch: int):
    return {
        Episode.CUR_OBS: np.random.random((batch, 2)),
        Episode.ACTION: np.random.random((batch,)),
        Episode.REWARD: np.random.random((batch,)),
    }


class TestEpisode:
    """
    Episode is responsible for the trajectory collection of a group of agents. We need to ensure that it supports two kinds of gait situations.

    1. All agents have the same episode length: this is an usual case;
    2. There is a difference between agents in the length of timestep.
    """

    @pytest.fixture(autouse=True)
    def init(self):
        self.env = DummyEnv(enable_env_state=True)
        self.episode = Episode(agents=self.env.possible_agents)

    def test_assignment_and_retrive(self):
        """This function tests the `getter` and `setter`"""

        # supose we write a transition to each agent, then we should retrive the same one for each agent
        x = {k: generate_a_transition(2) for k in self.env.possible_agents}
        for agent in self.env.possible_agents:
            self.episode[agent] = x[agent]

        for k, v in self.episode.agent_entry.items():
            assert v == x[k]

    def test_record(self):
        state, obs = self.env.reset()

        done = False
        while not done:
            actions = {
                k: action_space.sample()
                for k, action_space in self.env.action_spaces.items()
            }
            self.episode.record(
                {Episode.CUR_OBS: obs, Episode.CUR_STATE: state}, agent_first=False
            )
            state, obs, rew, dones, info = self.env.step(actions)
            done = dones.pop("__all__")
            self.episode.record(
                {Episode.REWARD: rew, Episode.DONE: dones, Episode.ACTION: actions},
                agent_first=False,
            )
        self.episode.record(
            {Episode.CUR_OBS: obs, Episode.CUR_STATE: state}, agent_first=False
        )

        for agent, agent_trans in self.episode.agent_entry.items():
            for k, v in agent_trans.items():
                if k in [Episode.CUR_OBS, Episode.CUR_STATE, Episode.ACTION_MASK]:
                    assert len(v) == self.env.max_step + 1, (len(v), self.env.max_step)
                else:
                    assert len(v) == self.env.max_step, (len(v), self.env.max_step)

        for agent, agent_trans in self.episode.to_numpy().items():
            for k, v in agent_trans.items():
                print(k, v.shape)
                assert v.shape[0] == self.env.max_step, (v.shape, self.env.max_step)

    def test_data_ordering(self):
        import ray
        import torch
        import gym

        from malib.backend.offline_dataset_server import OfflineDataset
        from malib.rl.dqn import DQNPolicy, DQNTrainer, DEFAULT_CONFIG
        from malib.rollout.envs.gym import GymEnv

        if not ray.is_initialized():
            ray.init(local_mode=False)

        dataset_server: OfflineDataset = OfflineDataset.as_remote().remote(
            table_capacity=10000
        )
        _, writer = ray.get(dataset_server.start_producer_pipe.remote("test"))
        _, reader = ray.get(
            dataset_server.start_consumer_pipe.remote(
                "test", DEFAULT_CONFIG["training_config"]["batch_size"]
            )
        )

        # buffers = {}

        # def sample(batch_size: int):
        #     res = {}
        #     if "obs" not in buffers:
        #         return None
        #     length = len(buffers["obs"])
        #     if length < batch_size:
        #         return None
        #     indices = np.random.choice(length, batch_size)
        #     for k, v in buffers.items():
        #         res[k] = v[indices]
        #     batch = Batch(res)
        #     batch.to_torch()
        #     return batch

        env = GymEnv(env_id="CartPole-v1")
        policy = DQNPolicy(
            observation_space=env.observation_spaces["agent"],
            action_space=env.action_spaces["agent"],
            model_config=DEFAULT_CONFIG["model_config"],
            custom_config=DEFAULT_CONFIG["custom_config"],
        )
        trainer = DQNTrainer(
            training_config=DEFAULT_CONFIG["training_config"], policy_instance=policy
        )

        num_episode = 10000
        cnt = 0

        for _ in range(num_episode):
            done = False

            episode = Episode(agents=["agent"])
            state, obs = env.reset(max_step=200)

            episode.record({Episode.CUR_OBS: obs}, agent_first=False)

            while not done:
                cnt += 1

                obs = {
                    k: torch.from_numpy(v).float().reshape(1, -1)
                    for k, v in obs.items()
                }
                actions = {
                    k: policy.compute_action(v, None, evaluate=False)[0][0]
                    for k, v in obs.items()
                }
                state, obs, rew, dones, info = env.step(actions)

                done = dones.pop("__all__")
                episode.record(
                    {
                        Episode.ACTION: actions,
                        Episode.CUR_OBS: obs,
                        Episode.REWARD: rew,
                        Episode.DONE: dones,
                    },
                    agent_first=False,
                )

                if cnt % 20 == 0:
                    # batch = sample(DEFAULT_CONFIG["training_config"]["batch_size"])
                    # if batch is not None:
                    #     loss_info = trainer(batch)
                    batch_info = reader.get()
                    if len(batch_info[-1]) == 0:
                        continue
                    loss_info = trainer(batch_info[0])

            agent_numpy = episode.to_numpy()
            writer.put_nowait_batch([agent_numpy["agent"]])
            # for k, v in agent_numpy.items():
            #     if k in buffers:
            #         buffers[k] = np.concatenate([buffers[k], v], axis=0)
            #     else:
            #         buffers[k] = v.copy()

            # run eval
            mean_episode_rew = 0.0
            mean_step_length = 0.0

            for _ in range(5):
                done = False
                episode_rew = 0.0
                eval_length = 0
                state, obs = env.reset(max_step=200)
                while not done:
                    eval_length += 1
                    obs = {
                        k: torch.from_numpy(v).float().reshape(1, -1)
                        for k, v in obs.items()
                    }
                    actions = {
                        k: policy.compute_action(v, None, evaluate=True)[0][0]
                        for k, v in obs.items()
                    }
                    state, obs, rew, dones, info = env.step(actions)
                    done = dones.pop("__all__")
                    episode_rew += rew["agent"]
                mean_episode_rew += episode_rew / 5
                mean_step_length += eval_length / 5

            print("mean_episode_rew:", mean_episode_rew, mean_step_length, policy.eps)
