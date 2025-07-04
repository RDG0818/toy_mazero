# model/model.py

import flax.linen as fnn
import jax
import jax.numpy as jnp
import chex
from model.attention import MLP, BaseAttention, TransformerAttentionEncoder
from typing import Tuple, NamedTuple, Optional
from config import ModelConfig

class MuZeroOutput(NamedTuple):
    hidden_state: chex.Array
    reward_logits: chex.Array
    policy_logits: chex.Array
    value_logits: chex.Array


class RepresentationNetwork(fnn.Module):
    """Encodes a local observation into a latent state for a single agent."""
    hidden_state_size: int
    fc_layers: Tuple[int, ...]

    @fnn.compact
    def __call__(self, observation: chex.Array) -> chex.Array:
        """
        Forward pass for the representation network.

        Args:
            observation (chex.Array): Local observation for an agent. Shape: (B, obs_dim)

        Returns:
            chex.Array: Encoded latent state. Shape: (B, hidden_state_size)
        """
        x = fnn.LayerNorm()(observation)
        x = MLP(layer_sizes=self.fc_layers, output_size=self.hidden_state_size)(x)
        return x


class DynamicsNetwork(fnn.Module):
    """Predicts the next latent state and joint reward."""
    hidden_state_size: int
    action_space_size: int
    reward_support_size: int
    fc_dynamic_layers: Tuple[int, ...]
    fc_reward_layers: Tuple[int, ...] 
    attention_module: Optional[BaseAttention] = None

    @fnn.compact
    def __call__(self, hidden_states: chex.Array, actions: chex.Array) -> Tuple[chex.Array, chex.Array]:
        """
        Forward pass for the dynamics network.

        Args:
            hidden_states (chex.Array): Latent states. Shape: (B, N, D_hidden)
            actions (chex.Array): Joint actions taken. Shape: (B, N)

        Returns:
            A tuple containing:
            - next_latent_states (chex.Array): Shape: (B, N, D_hidden)
            - reward_logits (chex.Array): Shape: (B, reward_support_size * 2 + 1)
        """
        batch_size, num_agents, _ = hidden_states.shape
        actions_onehot = jax.nn.one_hot(actions, num_classes=self.action_space_size)
        chex.assert_shape(actions_onehot, (batch_size, num_agents, None))

        # Next state prediction
        dynamic_input = jnp.concatenate([hidden_states, actions_onehot], axis=-1)

        if self.attention_module is not None:
            agent_context = self.attention_module(dynamic_input)
            flat_dynamic_input = agent_context.reshape(batch_size * num_agents, -1)
        else:
            flat_dynamic_input = dynamic_input.reshape(batch_size * num_agents, -1)

        dynamic_net = MLP(layer_sizes=self.fc_dynamic_layers, output_size=self.hidden_state_size)
        next_latent_states = dynamic_net(flat_dynamic_input).reshape(batch_size, num_agents, -1)
        next_latent_states += hidden_states  

        # Reward prediction
        reward_input = jnp.concatenate([next_latent_states, actions_onehot], axis=-1)
        flat_reward_input = reward_input.reshape(batch_size, -1)
        reward_output_size = self.reward_support_size * 2 + 1
        reward_net = MLP(layer_sizes=self.fc_reward_layers, output_size=reward_output_size)
        reward_logits = reward_net(flat_reward_input)

        return next_latent_states, reward_logits


class PredictionNetwork(fnn.Module):
    """Predicts the policy for each agent and the centralized value."""
    action_space_size: int
    value_support_size: int
    fc_value_layers: Tuple[int, ...]
    fc_policy_layers: Tuple[int, ...]

    @fnn.compact
    def __call__(self, hidden_states: chex.Array) -> Tuple[chex.Array, chex.Array]:
        """
        Forward pass for the prediction network.

        Args:
            hidden_states (chex.Array): Latent states. Shape: (B, N, D_hidden)

        Returns:
            A tuple containing:
            - policy_logits (chex.Array): Shape: (B, N, A)
            - value_logits (chex.Array): Shape: (B, value_support_size * 2 + 1)
        """
        batch_size, num_agents, _ = hidden_states.shape

        # Value prediction
        flat_hidden_states = hidden_states.reshape(batch_size, -1)
        value_output_size = self.value_support_size * 2 + 1
        value_net = MLP(layer_sizes=self.fc_value_layers, output_size=value_output_size)
        value_logits = value_net(flat_hidden_states)

        # Policy prediction
        flat_agent_states = hidden_states.reshape(batch_size * num_agents, -1)
        policy_net = MLP(layer_sizes=self.fc_policy_layers, output_size=self.action_space_size)
        policy_logits = policy_net(flat_agent_states).reshape(batch_size, num_agents, -1)

        return policy_logits, value_logits


class ProjectionNetwork(fnn.Module):
    """
    A network for self-supervised learning, inspired by SimSiam.

    This network consists of two sub-networks:
    1. A projection MLP that maps hidden states to a latent projection space.
    2. A prediction MLP (or "head") that tries to predict the projection of a
       future state from the projection of a current state.
    """
    projection_hidden_dim: int
    projection_output_dim: int
    prediction_hidden_dim: int
    prediction_output_dim: int

    @fnn.compact
    def __call__(self, x: chex.Array) -> chex.Array:
        """
        Projects the input hidden state into the latent projection space.

        Args:
            x (chex.Array): The input hidden state, typically of shape (batch_size, hidden_state_dim).

        Returns:
            chex.Array: The resulting projection.
        """
        
        projection = MLP(
            layer_sizes=[self.projection_hidden_dim],
            output_size=self.projection_output_dim,
            name="projection_mlp"
        )(x)
        projection = fnn.LayerNorm()(projection)
        return projection

    @fnn.compact
    def predict(self, proj: chex.Array) -> chex.Array:
        """
        Predicts the future state from the projected current state.

        Args:
            proj (chex.Array): The projected current state, typically of shape (batch_size, projection_output_dim).

        Returns:
            chex.Array: The predicted future state.
        """
        prediction = MLP(
            layer_sizes=[self.prediction_hidden_dim],
            output_size=self.prediction_output_dim,
            name="prediction_mlp"
        )(proj)
        return prediction


class FlaxMAMuZeroNet(fnn.Module):
    config: ModelConfig
    action_space_size: int

    def setup(self):
        attention_module = None
        if self.config.attention_type == "transformer":
            attention_module = TransformerAttentionEncoder(
                num_layers=self.config.attention_layers,
                num_heads=self.config.attention_heads,
                hidden_size=self.config.hidden_state_size,
                dropout_rate=self.config.dropout_rate
            )
        self.representation_net = RepresentationNetwork(
            hidden_state_size=self.config.hidden_state_size,
            fc_layers=self.config.fc_representation_layers
        )
        self.dynamics_net = DynamicsNetwork(
            hidden_state_size=self.config.hidden_state_size,
            action_space_size=self.action_space_size,
            reward_support_size=self.config.reward_support_size,
            fc_dynamic_layers=self.config.fc_dynamic_layers,
            fc_reward_layers=self.config.fc_reward_layers,
            attention_module=attention_module
        )
        self.prediction_net = PredictionNetwork(
            action_space_size=self.action_space_size,
            value_support_size=self.config.value_support_size,
            fc_value_layers=self.config.fc_value_layers,
            fc_policy_layers=self.config.fc_policy_layers
        )
        self.projection_net = ProjectionNetwork(
            projection_hidden_dim=self.config.proj_hid,
            projection_output_dim=self.config.proj_out,
            prediction_hidden_dim=self.config.pred_hid,
            prediction_output_dim=self.config.pred_out
        )

    def __call__(self, observations: chex.Array) -> MuZeroOutput:
        """
        Initial inference step.

        Args:
            observations (chex.Array): Batch of observations. Shape: (B, N, obs_dim)

        Returns:
            MuZeroOutput: A structured object containing:
                - hidden_state: The encoded latent state. Shape: (B, N, D_hidden)
                - reward_logits: A zero array, placeholder for the initial step.
                - policy_logits: The predicted policy logits. Shape: (B, N, A)
                - value_logits: The predicted value logits. Shape: (B, V_support)
        """
        batch_size, num_agents, _ = observations.shape

        flat_obs = observations.reshape(batch_size * num_agents, -1)
        hidden_states = self.representation_net(flat_obs).reshape(batch_size, num_agents, -1)

        policy_logits, value_logits = self.prediction_net(hidden_states)
        reward_logits = jnp.zeros((batch_size, self.config.reward_support_size * 2 + 1))

        if self.is_mutable_collection('params'): # Initialize dynamics network parameters
            dummy_actions = jnp.zeros((batch_size, num_agents), dtype=jnp.int32)
            self.dynamics_net(hidden_states, dummy_actions)
            self.project(hidden_states, with_prediction_head=True)
            self.project(hidden_states, with_prediction_head=False)

        return MuZeroOutput(
            hidden_state=hidden_states,
            reward_logits=reward_logits,
            policy_logits=policy_logits,
            value_logits=value_logits
        )

    def recurrent_inference(self, hidden_states: chex.Array, actions: chex.Array) -> MuZeroOutput:
        """
        Projects a latent state forward in time using an action.

        Args:
            hidden_states (chex.Array): Current latent states. Shape: (B, N, D_hidden)
            actions (chex.Array): Joint actions taken. Shape: (B, N)

        Returns:
            MuZeroOutput: A structured object containing:
                - hidden_state: The next latent state from the dynamics model.
                - reward_logits: The predicted reward logits for the state transition.
                - policy_logits: The policy logits for the next state.
                - value_logits: The value logits for the next state.
        """

        next_hidden_states, reward_logits = self.dynamics_net(hidden_states, actions)

        policy_logits, value_logits = self.prediction_net(next_hidden_states)

        return MuZeroOutput(
            hidden_state=next_hidden_states,
            reward_logits=reward_logits,
            policy_logits=policy_logits,
            value_logits=value_logits
        )

    def predict(self, hidden_states: chex.Array) -> Tuple[chex.Array, chex.Array]:
        policy_logits, value_logits = self.prediction_net(hidden_states)
        return policy_logits, value_logits
    
    def project(self, hidden_state: chex.Array, with_prediction_head: bool = True) -> chex.Array:
        """
        Projects the hidden state into the latent space for self-supervised loss.
        Args:
            hidden_state (chex.Array): The latent state from the representation or dynamics network.
            with_prediction_head (bool): If True, applies the prediction head after the
                projection. This is used for the "online" network branch. Defaults to True.

        Returns:
            chex.Array: The final projected and optionally predicted vector.
        """
        proj = self.projection_net(hidden_state)
        if with_prediction_head:
            # This branch is for the "online" network which has gradients flowing through it
            return self.projection_net.predict(proj)
        else:
            # This branch is for the "target" network, gradients will be stopped later
            return proj