"""
Implementation of A2C by the Google DeepMind team.
Reference:
    "Asynchronous Methods for Deep Reinforcement Learning" by Mnih et al. 
"""

import os
import math
import random
import torch
from torch.distributions.categorical import Categorical
import numpy as np 
from tensorboardX import SummaryWriter
from collections import namedtuple

from game.wrapper import Game 

# Global parameter which tells us if we have detected a CUDA capable device
CUDA_DEVICE = torch.cuda.is_available()

class ActorCriticNetwork(torch.nn.Module):

    def __init__(self, options):
        """
        Initialize an ActorCriticNetworkself.opt instance. The actor has an output for 
        each action and the critic provides the value output
        Uses the same parameters as specified in the paper.
        """
        super(ActorCriticNetwork, self).__init__()

        self.opt = options
        
        self.conv1 = torch.nn.Conv2d(self.opt.len_agent_history, 16, 8, 4)
        self.relu1 = torch.nn.ReLU()
        self.conv2 = torch.nn.Conv2d(16, 32, 4, 2)
        self.relu2 = torch.nn.ReLU()
        self.fc3 = torch.nn.Linear(2592, 256) # TODO: Don't hard code
        self.relu3 = torch.nn.ReLU()
        self.actor = torch.nn.Linear(256, self.opt.n_actions)
        self.critic = torch.nn.Linear(256, 1)
        self.softmax = torch.nn.Softmax()
        self.logsoftmax = torch.nn.LogSoftmax()


    def init_weights(self, m):
        """
        Initialize the weights of the network.

        Arguments:
            m (tensor): layer instance 
        """
        if type(m) == torch.nn.Conv2d or type(m) == torch.nn.Linear:
            torch.nn.init.uniform(m.weight, -0.01, 0.01)
            m.bias.data.fill_(0.01)


    def forward(self, x):
        """
        Forward pass to compute Q-values for given input states.

        Arguments:
            x (tensor): minibatch of input states

        Returns:
            int: selected action, 0 to do nothing and 1 to flap
            float: entropy of action space
            float: log probability of selecting the action 
            float: value of the particular state
        """
        # Forward pass
        x = self.relu1(self.conv1(x))
        x = self.relu2(self.conv2(x))
        x = x.view(x.size()[0], -1)
        x = self.relu3(self.fc3(x))
        action_logits = self.actor(x)
        value = self.critic(x)
        return value, action_logits


    def act(self, x):
        """
        Returns:
            tensor(8,1)
        """
        # Forward pass
        values, action_logits = self.forward(x)
        probs = self.softmax(action_logits)
        log_probs = self.logsoftmax(action_logits)

        # Choose action stochastically
        actions = probs.multinomial(1)

        # Evaluate action
        action_log_probs = log_probs.gather(1, actions)
        dist_entropy = -(log_probs * probs).sum(-1).mean()
        return values, actions, action_log_probs

    def evaluate_actions(self, x, actions):
        # Forward pass 
        value, action_logits = self.forward(x)
        probs = self.softmax(action_logits)
        log_probs = self.logsoftmax(action_logits)

        # Evaluate actions
        action_log_probs = log_probs.gather(1, actions)
        dist_entropy = -(log_probs * probs).sum(-1).mean()
        return value, action_log_probs, dist_entropy


Experience = namedtuple('Experience', ('state', 'action', 'action_log_prob', 'value', 'reward', 'mask'))

class A2CAgent():

    def __init__(self, options):
        """
        Initialize an A2C Instance. 
        """
        self.opt = options

        # Create ACNetwork
        self.net = ActorCriticNetwork(self.opt)
        self.net.apply(self.net.init_weights)
        if self.opt.mode == 'train':
            self.net.apply(self.net.init_weights)
            if self.opt.weights_dir:
                self.net.load_state_dict(torch.load(self.opt.weights_dir))
        if self.opt.mode == 'eval':
            self.net.load_state_dict(torch.load(self.opt.weights_dir))
            self.net.eval()
        
        if CUDA_DEVICE:
            self.net = self.net.cuda()

        # Optimizer
        self.optimizer = torch.optim.Adam(self.net.parameters(), lr=self.opt.learning_rate)

        # The flappy bird game instance
        self.games = [Game(self.opt.frame_size) for i in range(self.opt.n_workers)]

        # Log to tensorBoard
        self.writer = SummaryWriter(self.opt.exp_name)

        # Buffer
        self.memory = []


    def optimize_model(self):
        """
        Performs a single step of optimization.

        Arguments:
            next_state (tensor): next frame of the game
            done (bool): True if next_state is a terminal state, else False

        Returns:
            loss (float)
        """
        # Transpose the batch (see https://stackoverflow.com/a/19343/3343043 for
        # detailed explanation). This converts batch-array of Transitions
        # to Transition of batch-arrays.
        memory = Experience(*zip(*self.memory))

        batch = {
            'state': torch.stack(memory.state),
            'action': torch.stack(memory.action),
            'reward': torch.tensor(memory.reward),
            'mask': torch.stack(memory.mask)
        }
        state_shape = batch['state'].size()[2:]
        action_shape = batch['action'].size()[-1]

        # Calculate the value of the next state
        next_value, _ = self.net(batch['state'][-1])

        # Compute returns
        returns = torch.zeros(self.opt.buffer_update_freq + 1, self.opt.n_workers, 1)
        returns[-1] = next_value
        for i in reversed(range(self.opt.buffer_update_freq)):
            returns[i] = returns[i+1] * self.opt.discount_factor * batch['mask'][i] + batch['reward'][i]
        returns = returns[:-1]

        # Evaluate actions
        values, action_log_probs, dist_entropy = self.net.evaluate_actions(batch['state'].view(-1, *state_shape), batch['action'].view(-1, action_shape)) ### HERE
        values = values.view(self.opt.buffer_update_freq, self.opt.n_workers, 1)
        action_log_probs = action_log_probs.view(self.opt.buffer_update_freq, self.opt.n_workers, 1)

        # Compute losses
        advantages = returns - values
        value_loss = advantages.pow(2).mean()
        action_loss = -(advantages * action_log_probs).mean()
        loss = value_loss * self.opt.value_loss_coeff + action_loss - dist_entropy * self.opt.entropy_coeff

        # Optimizer step
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm(self.net.parameters(), self.opt.max_grad_norm)
        self.optimizer.step()

        return loss, value_loss * self.opt.value_loss_coeff, action_loss, -dist_entropy * self.opt.entropy_coeff


    def env_step(self, states, actions):
        next_state_list, reward_list, done_list = [], [], []
        for i in range(self.opt.n_workers):
            frame, reward, done = self.games[i].step(actions[i])
            if states is None:
                next_state = torch.cat([frame for i in range(self.opt.len_agent_history)])
            else:
                next_state = torch.cat([states[i][1:], frame])
            next_state_list.append(next_state)
            reward_list.append([reward])
            done_list.append(done)

        return torch.stack(next_state_list), reward_list, done_list


    def train(self):
        """
        Main training loop.
        """
        # Episode lengths
        episode_lengths = np.zeros(self.opt.n_workers)

        # Initialize the environment and state (do nothing)
        initial_actions = np.zeros(self.opt.n_workers)
        states, _, _ = self.env_step(None, initial_actions)

        # Start a training episode
        for i in range(1, self.opt.n_train_iterations):

            # Forward pass through the net
            values, actions, action_log_probs = self.net.act(states)

            # Perform action in environment
            next_states, rewards, dones = self.env_step(states, actions)
            masks = torch.FloatTensor([[0.0] if done else [1.0] for done in dones])

            # Save experience to buffer
            self.memory.append(
                Experience(states.data, actions.data, action_log_probs.data, values.data, rewards, masks)
            )

            # Perform optimization
            if i % self.opt.buffer_update_freq == 0:
                loss, value_loss, action_loss, entropy_loss = self.optimize_model()
                # Reset memory
                self.memory = []

            # Log episode length
            for j in range(self.opt.n_workers):
                if not dones[j]:
                    episode_lengths[j] += 1
                else:
                    self.writer.add_scalar('episode_length/' + str(j), episode_lengths[j], i)
                    print(j, episode_lengths[j])
                    episode_lengths[j] = 0

            # Save network
            if i % self.opt.save_frequency == 0:
                if not os.path.exists(self.opt.exp_name):
                    os.mkdir(self.opt.exp_name)
                torch.save(self.net.state_dict(), f'{self.opt.exp_name}/{str(i).zfill(7)}.pt')

            # Write results to log
            if i % self.opt.log_frequency == 0:
                self.writer.add_scalar('loss/total', loss, i)
                self.writer.add_scalar('loss/action', action_loss, i)
                self.writer.add_scalar('loss/value', value_loss, i)
                self.writer.add_scalar('loss/entropy', entropy_loss, i)                

            # Move on to next state
            states = next_states


    def play_game(self):
        """
        Play Flappy Bird using the trained network.
        """

        # Initialize the environment and state (do nothing)
        self.game = self.games[0]
        frame, reward, done = self.game.step(0)
        state = torch.cat([frame for i in range(self.opt.len_agent_history)])

        # Start playing
        while True:

            # Perform an action
            state = state.unsqueeze(0)
            if CUDA_DEVICE:
                state = state.cuda()
            _, action, _ = self.net.act(state)
            if CUDA_DEVICE:
              action = action.cuda()
            frame, reward, done = self.game.step(action)
            if CUDA_DEVICE:
                frame = frame.cuda()
            next_state = torch.cat([state[0][1:], frame])

            # Move on to the next state
            state = next_state

            # If we lost, exit
            if done:
                break