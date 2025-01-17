# MIT License

# Copyright (c) 2021 MARL @ SJTU

# Author: Ming Zhou

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Test agent interface start up here"""

from types import LambdaType
from typing import Dict, Any, Dict, List, Set, Tuple, Type

import os
import pytest
import ray

from collections import defaultdict

from malib import settings
from malib.utils.typing import AgentID
from malib.agent import IndependentAgent
from malib.agent.manager import TrainingManager
from malib.scenarios.marl_scenario import MARLScenario
from malib.rl.random import RandomPolicy, RandomTrainer, DEFAULT_CONFIG
from malib.backend.offline_dataset_server import OfflineDataset
from malib.backend.parameter_server import ParameterServer


def default_algorithms():
    return {"default": (RandomPolicy, RandomTrainer, {}, {})}


def generate_gym_desc(env_id):
    from malib.rollout.envs.gym import env_desc_gen

    return env_desc_gen(env_id=env_id, scenario_configs={})


def agent_mapping_one_to_one(
    possible_agents: List[AgentID],
) -> Tuple[LambdaType, Dict[str, Set]]:
    func = lambda agent: agent
    agent_groups = defaultdict(lambda: set())
    for agent in possible_agents:
        rid = func(agent)
        agent_groups[rid].add(agent)
    return func, dict(agent_groups)


@pytest.mark.parametrize("algorithms", [default_algorithms()])
@pytest.mark.parametrize("env_desc", [generate_gym_desc("CartPole-v1")])
@pytest.mark.parametrize(
    "training_type,custom_training_config", [(IndependentAgent, {})]
)
class TestTrainingManager:
    @pytest.fixture(autouse=True)
    def init(
        self,
        algorithms: Dict[str, Any],
        env_desc: Dict[str, Any],
        training_type: Type,
        custom_training_config: Dict[str, Any],
        remote_mode: bool = True,
    ):
        """Initialization interface test for TrainingManager"""

        agent_mapping_func, target_agent_groups = agent_mapping_one_to_one(
            env_desc["possible_agents"]
        )
        log_dir = "/tmp/malib/test_manager"

        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        if not ray.is_initialized():
            ray.init()

        try:
            offline_dataset_server = OfflineDataset.options(
                name=settings.OFFLINE_DATASET_ACTOR
            ).remote(table_capacity=100)
        except ValueError:
            print("detected existing offline dataset server")
            offline_dataset_server = ray.get_actor(settings.OFFLINE_DATASET_ACTOR)

        try:
            parameter_server = ParameterServer.options(
                name=settings.PARAMETER_SERVER_ACTOR
            ).remote()
        except ValueError:
            print("detected exisitng parameter server")
            parameter_server = ray.get_actor(settings.PARAMETER_SERVER_ACTOR)

        ray.get(
            [parameter_server.start.remote(), offline_dataset_server.start.remote()]
        )

        training_config = {
            "type": training_type,
            "trainer_config": DEFAULT_CONFIG["training_config"],
            "custom_config": custom_training_config,
        }

        self.training_manager = TrainingManager(
            experiment_tag="test_training_manager",
            algorithms=algorithms,
            env_desc=env_desc,
            agent_mapping_func=agent_mapping_func,
            training_config=training_config,
            log_dir=log_dir,
            remote_mode=remote_mode,
        )

        # check agent groups
        assert self.training_manager.agent_groups == target_agent_groups, (
            self.training_manager.agent_groups,
            target_agent_groups,
        )
        # check agent interfaces
        agent_interfaces = self.training_manager._interfaces
        assert set(agent_interfaces.keys()) == set(target_agent_groups.keys()), (
            agent_interfaces.keys(),
            target_agent_groups.keys(),
        )

    def test_policy_add(self):
        """Test policy adding function"""

        self.training_manager.add_policies(n=1)
