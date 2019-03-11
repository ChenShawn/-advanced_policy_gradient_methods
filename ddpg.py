import tensorflow as tf
import gym
from tensorflow.contrib import slim
from gym.wrappers import Monitor
import argparse
import numpy as np
import os
import threading

from utils import save, load, AgentBase

""" ======= DEEP DETERMINISTIC POLICY GRADIENT =======
1. For better exploration, the behavior policy in DDPG contains noise 
   generated by Ornstein-Uhlenbeck process, i.e. exponential decay
   
2. Update both policy and Q function using exponential moving average 
   instead of using `tf.assign`

3. Observe that both rolling event and updating the model 
   requires only the interaction with the buffer, thus uses 
   multi-threads to implement the algorithm

================== GLOBAL VARIABLES ================== """
GAMMA = 0.9
TAU = 0.01
VAR_DECAY = 0.995
A_LR = 1e-3
C_LR = 2e-3
A_ITER = 3
C_ITER = 1
A_DIM = 1
S_DIM = 3

BATCH_SIZE = 128
EP_MAXLEN = 200
N_ITERS = 500000
CAPACITY = 10000
WRITE_LOGS_EVERY = 200
LOGDIR = './logs/ddpg/'
MODEL_DIR = './ckpt/ddpg/'

RENDER = True
LOCK = threading.Lock()

""" ================================================== """

class EMAGetter(object):
    ema = tf.train.ExponentialMovingAverage(decay=1.0 - TAU)

    def __call__(self, getter, name, *args, **kwargs):
        return self.ema.average(getter(name, *args, **kwargs))


class MemoryBuffer(object):
    def __init__(self, capacity, s_dim):
        self.capacity = capacity
        self.state = np.zeros((capacity, s_dim), dtype=np.float32)
        self.reward = np.zeros((capacity, 1), dtype=np.float32)
        self.s_next = np.zeros((capacity, s_dim), dtype=np.float32)
        self.pointer = 0

    def init_buffer(self, env, agent):
        while True:
            s = env.reset()
            for it in range(EP_MAXLEN):
                s_next, r, done, info = env.step(agent.choose_action(s))
                r = (r + 8.0) / 8.0
                self.store_transition(s, r, s_next)
                if self.pointer == 0:
                    return
                if done:
                    break

    def store_transition(self, s, r, s_next):
        self.state[self.pointer, :] = s
        self.reward[self.pointer, :] = r
        self.s_next[self.pointer, :] = s_next
        self.pointer = (self.pointer + 1) % self.capacity

    def sample(self, num):
        indices = np.random.randint(0, self.capacity, size=[num])
        return self.state[indices], self.reward[indices], self.s_next[indices]



class DDPGModel(AgentBase):
    name = 'DDPGModel'

    def __init__(self, s_dim, a_dim):
        self.state = tf.placeholder(tf.float32, [None, s_dim], name='state')
        self.s_next = tf.placeholder(tf.float32, [None, s_dim], name='s_next')
        self.reward = tf.placeholder(tf.float32, [None, 1], name='discounted_r')

        self.ema_getter = EMAGetter()
        with tf.variable_scope('DDPG'):
            self.actor = self._build_policy(self.state, 'Actor', a_dim)
            self.target_q = self._build_q_network(self.state, self.actor, 'Critic')

        a_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='DDPG/Actor')
        c_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='DDPG/Critic')
        target_update = [self.ema_getter.ema.apply(a_vars), self.ema_getter.ema.apply(c_vars)]

        with tf.variable_scope('DDPG'):
            self.a_next = self._build_policy(self.s_next, 'Actor', a_dim, trainable=False,
                                             reuse=True, custom_getter=self.ema_getter)
            self.eval_q = self._build_q_network(self.s_next, self.a_next, 'Critic', trainable=False,
                                                reuse=True, custom_getter=self.ema_getter)

        a_loss = -tf.reduce_mean(self.target_q)
        self.a_optim = tf.train.AdamOptimizer(A_LR).minimize(a_loss, var_list=a_vars)

        with tf.control_dependencies(target_update):
            td_error = tf.losses.mean_squared_error(labels=self.reward + GAMMA * self.eval_q,
                                                    predictions=self.target_q)
            self.c_optim = tf.train.AdamOptimizer(C_LR).minimize(td_error, var_list=c_vars)

        self.counter = 0
        self.variance = 3.0

        self.sums = tf.summary.merge([
            tf.summary.scalar('reward', tf.reduce_mean(self.reward)),
            tf.summary.scalar('actor_loss', a_loss),
            tf.summary.scalar('critic_loss', td_error),
            tf.summary.histogram('Q_taregt', self.target_q)
        ], name='summaries')

        self.sess = tf.Session()
        self.sess.run(tf.global_variables_initializer())
        print(' [*] Build DDPGModel finished...')


    def _build_policy(self, state, scope, a_dim, trainable=True, reuse=False, custom_getter=None):
        with tf.variable_scope(scope, reuse=reuse, custom_getter=custom_getter):
            net = tf.layers.dense(state, 64, activation=tf.nn.relu, trainable=trainable, name='h1')
            action = 2.0 * tf.layers.dense(net, a_dim, activation=tf.nn.tanh, trainable=trainable, name='h2')
        return action


    def _build_q_network(self, state, action, scope, trainable=True, reuse=False, custom_getter=None):
        with tf.variable_scope(scope, reuse=reuse, custom_getter=custom_getter):
            h1 = tf.layers.dense(state, 64, activation=None, trainable=trainable, name='h1')
            h2 = tf.layers.dense(action, 64, activation=None, use_bias=False, trainable=trainable, name='h2')
            h3 = tf.nn.relu(h1 + h2)
            return tf.layers.dense(h3, 1, trainable=trainable, use_bias=True, activation=None, name='h3')


    def train(self, s, r, s_next, callback=None):
        feed_dict = {self.state: s, self.reward: r, self.s_next: s_next}
        for _ in range(C_ITER):
            self.sess.run(self.c_optim, feed_dict=feed_dict)
        # for _ in range(A_ITER):
        #     self.sess.run(self.a_optim, feed_dict=feed_dict)
        self.counter += 1
        if self.counter % WRITE_LOGS_EVERY == (WRITE_LOGS_EVERY - 1):
            self.variance *= VAR_DECAY
            print('Global step {}, current variance in behavior policy {}'.format(self.counter, self.variance))
        if callback is not None:
            callback(self.sums, feed_dict)


    def choose_action(self, s):
        a = self.sess.run(self.actor, feed_dict={self.state: s[None, :]})[0]
        var = np.random.normal(0.0, self.variance, size=a.shape)
        return np.clip(a + var, -2.0, 2.0)


    def get_value(self, s):
        return self.sess.run(self.target_q, feed_dict={self.state: s[None, :]})



class CallbackFunctor(object):
    def __init__(self, logdir):
        self.writer = tf.summary.FileWriter(logdir, model.sess.graph)
        self.counter = 0

    def __call__(self, sums, feed_dict):
        if self.counter % WRITE_LOGS_EVERY == 5:
            sumstr = model.sess.run(sums, feed_dict=feed_dict)
            self.writer.add_summary(sumstr, global_step=self.counter)
        self.counter += 1



if __name__ == '__main__':
    if not os.path.exists(LOGDIR):
        os.makedirs(LOGDIR)
    if not os.path.exists(MODEL_DIR):
        os.makedirs(MODEL_DIR)

    model = DDPGModel(S_DIM, A_DIM)
    buffer = MemoryBuffer(CAPACITY, S_DIM)
    coord = tf.train.Coordinator()
    _, model.counter = load(model.sess, model_path=MODEL_DIR)
    slim.model_analyzer.analyze_vars(tf.trainable_variables(), print_info=True)

    class ModelThread(threading.Thread):
        def __init__(self, wid=0):
            self.wid = wid
            self.functor = CallbackFunctor(logdir=LOGDIR)
            super(ModelThread, self).__init__()
            print(' [*] ModelThread wid {} okay...'.format(wid))

        def run(self):
            print(' [*] ModelThread start to run...')
            for it in range(N_ITERS):
                LOCK.acquire()
                s, r, s_next = buffer.sample(BATCH_SIZE)
                LOCK.release()
                model.train(s, r, s_next, callback=self.functor)
            coord.request_stop()
            print(' [*] ModelThread wid {} reaches the exit!'.format(self.wid))


    class BufferThread(threading.Thread):
        def __init__(self, wid=1, render=True):
            self.render = render
            self.wid = wid
            self.env = gym.make('Pendulum-v0').unwrapped
            self.env.seed(1)
            super(BufferThread, self).__init__()
            print(' [*] BufferThread {} okay...'.format(wid))

        def run(self):
            print(' [*] BufferThread start to run...')
            while not coord.should_stop():
                s = self.env.reset()
                gamma = 1.0
                for it in range(EP_MAXLEN):
                    if self.render:
                        self.env.render()
                    s_next, r, done, info = self.env.step(model.choose_action(s))
                    r = (r + 8.0) / 8.0
                    LOCK.acquire()
                    buffer.store_transition(s, r, s_next)
                    LOCK.release()
                    s = s_next
                    if done:
                        break
            print(' [*] BufferThread wid {} reaches the exit!'.format(self.wid))


    model_thread = ModelThread()
    buffer_thread = BufferThread(render=RENDER)
    buffer.init_buffer(buffer_thread.env, model)

    model_thread.start()
    buffer_thread.start()
    coord.join([model_thread, buffer_thread])
    save(model.sess, MODEL_DIR, model.name, global_step=model.counter)
    print(' [*] The main process reaches the exit!!')