# Copyright 2018 D-Wave Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest
from parameterized import parameterized

import dimod
from dwave.system.testing import MockDWaveSampler
import hybrid
from hybrid.samplers import QPUSubproblemAutoEmbeddingSampler
from hybrid.reference.kerberos import KerberosSampler
from hybrid.reference.lattice_lnls import LatticeLNLSSampler
from hybrid.reference.lattice_lnls import LatticeLNLS
from hybrid.decomposers import make_origin_embeddings
from hybrid.reference.pa import (
    EnergyWeightedResampler, ProgressBetaAlongSchedule,
    CalculateAnnealingBetaSchedule, PopulationAnnealing
)


class MockDWaveSamplerGeneralization(MockDWaveSampler):
    """Extend the `dwave.system.testing.MockDWaveSampler` to Pegasus topology.
    
    Adding topology and shape keywords to MockDWaveSampler for this purpose.
    This function is mirrored in test_decomposers.py

    MockDWaveSampler() in the latest version of dwave-system support these 
    options, this function is included to support backward compatibility of the
    dwave-system package.
    """
    def __init__(self, broken_nodes=None, topology_type=None, qpu_scale=4, **config):
        import dwave_networkx as dnx
    
        super().__init__(broken_nodes, **config)
        #An Advantage generation processor, only artificially smaller,
        #replaces C4 in default MockDWaveSampler
        if topology_type != 'chimera':
            self.properties['topology'] = {'type': 'pegasus',
                                           'shape': [qpu_scale]}
            qpu_graph = dnx.pegasus_graph(qpu_scale,fabric_only=True)
        else:
            self.properties['topology'] = {'type': 'chimera',
                                           'shape': [qpu_scale,qpu_scale,4]}
            qpu_graph = dnx.chimera_graph(qpu_scale)
            
        #Adjust edge_list, 
        if broken_nodes is None:
            self.nodelist = sorted(qpu_graph.nodes)
            self.edgelist = sorted(tuple(sorted(edge))
                                   for edge in qpu_graph.edges)
        else:
            self.nodelist = sorted(v for v in qpu_graph.nodes
                                   if v not in broken_nodes)
            self.edgelist = sorted(tuple(sorted((u, v)))
                                   for u, v in qpu_graph.edges
                                   if u not in broken_nodes
                                   and v not in broken_nodes)
        self.properties['num_qubits'] = len(qpu_graph)
        self.properties['qubits'] = self.nodelist
        self.properties['couplers'] = self.edgelist
        

class TestLatticeLNLS(unittest.TestCase):
    
    def test_basic_workflow_operation(self):
        for topology_type in ['pegasus','chimera']:
            qpu_sampler=MockDWaveSamplerGeneralization(topology_type=topology_type)
            for lattice_type in ['cubic',topology_type]:
                LatticeLNLS(topology=lattice_type, qpu_sampler=qpu_sampler)
                
    def test_basic_sampler_operation(self):
        bqm = dimod.BinaryQuadraticModel({(i,j,k) : 0 for i in range(2) for j in range(2) for k in range(2)}, {((0,0,0),(0,0,1)): 1, ((1,1,0),(1,1,1)): 1}, 0, dimod.SPIN)
        sampleset = LatticeLNLSSampler().sample(
            bqm=bqm, problem_dims=(2,2,2), qpu_sampler=MockDWaveSamplerGeneralization(), topology='cubic',max_iter=1,
            qpu_params=dict(chain_strength=2), reject_small_problems=False)


class TestKerberos(unittest.TestCase):

    def test_basic(self):
        bqm = dimod.BinaryQuadraticModel({}, {'ab': 1, 'bc': 1, 'ca': 1}, 0, dimod.SPIN)
        KerberosSampler().sample(
            bqm, max_subproblem_size=1, qpu_sampler=MockDWaveSampler(),
            qpu_params=dict(chain_strength=2))

    def test_init_state(self):
        bqm = dimod.BQM.from_qubo({(0, 1): 1})
        init = dimod.SampleSet.from_samples_bqm([0, 0], bqm)
        KerberosSampler().sample(
            bqm, qpu_sampler=MockDWaveSampler(), init_sample=init)


class TestWeightedResampler(unittest.TestCase):

    def test_sampling(self):
        # for all practical purposes the distribution of energies here should
        # guarantee the last sample always wins
        winner = {'a': 1, 'b': 1}
        skewed = dimod.SampleSet.from_samples(
            [{'a': 0, 'b': 0}, {'a': 0, 'b': 1}, {'a': 1, 'b': 0}, winner],
            energy=[100, 100, 100, -100], vartype='BINARY')

        state = hybrid.State(samples=skewed)

        # cold sampling
        res = EnergyWeightedResampler(delta_beta=1, seed=1234).run(state).result()
        samples = res.samples.aggregate()

        self.assertEqual(len(samples), 1)
        self.assertDictEqual(samples.first.sample, winner)

        # hot sampling
        res = EnergyWeightedResampler(delta_beta=0, seed=1234).run(state).result()
        samples = res.samples.aggregate()

        self.assertGreater(len(samples), 1)

    def test_beta_use(self):
        ss = dimod.SampleSet.from_samples([{'a': 0}], energy=[0], vartype='SPIN')
        state = hybrid.State(samples=ss)

        # beta not given at all
        with self.assertRaises(ValueError):
            res = EnergyWeightedResampler().run(state).result()

        # beta given on construction
        res = EnergyWeightedResampler(delta_beta=0).run(state).result()
        self.assertEqual(res.samples.info['delta_beta'], 0)

        # beta given on runtime, to run method
        res = EnergyWeightedResampler().run(state, delta_beta=1).result()
        self.assertEqual(res.samples.info['delta_beta'], 1)

        # beta given in state
        state.delta_beta = 2
        res = EnergyWeightedResampler().run(state).result()
        self.assertEqual(res.samples.info['delta_beta'], 2)


class TestPopulationAnnealingUtils(unittest.TestCase):

    def test_beta_progressor(self):
        beta_schedule = [1, 2, 3]

        prog = ProgressBetaAlongSchedule(beta_schedule=beta_schedule)

        betas = []
        while True:
            try:
                betas.append(prog.run(hybrid.State()).result().beta)
            except:
                break

        self.assertEqual(betas, beta_schedule)

    def test_beta_schedule_calc_smoketest(self):
        bqm = dimod.BinaryQuadraticModel.from_ising({'a': 1}, {})
        state = hybrid.State.from_problem(bqm)

        # linear interp
        calc = CalculateAnnealingBetaSchedule(length=10, interpolation='linear')
        res = calc.run(state).result()
        self.assertIn('beta_schedule', res)
        self.assertEqual(len(res.beta_schedule), 10)

        # geometric interp
        calc = CalculateAnnealingBetaSchedule(length=10, interpolation='geometric')
        res = calc.run(state).result()
        self.assertIn('beta_schedule', res)
        self.assertEqual(len(res.beta_schedule), 10)

        # user-provided range
        calc = CalculateAnnealingBetaSchedule(
            length=3, interpolation='linear', beta_range=[0, 1])
        res = calc.run(state).result()
        self.assertIn('beta_schedule', res)
        self.assertEqual(list(res.beta_schedule), [0, 0.5, 1])


class TestPopulationAnnealing(unittest.TestCase):

    def test_smoke(self):
        bqm = dimod.BinaryQuadraticModel.from_ising({}, {'ab': 1})
        state = hybrid.State.from_problem(bqm)

        pa = PopulationAnnealing()
        ss = pa.run(state).result().samples

        self.assertEqual(ss.first.energy, -1)

    def test_range(self):
        bqm = dimod.BinaryQuadraticModel({0: -1, 1: 0.01}, {}, 0, 'BINARY')
        ground = {0: 1, 1: 0}
        state = hybrid.State.from_problem(bqm)

        pa = PopulationAnnealing(num_reads=1, num_iter=10, num_sweeps=100)
        ss = pa.run(state).result().samples

        self.assertDictEqual(ss.first.sample, ground)

    def test_custom_beta_schedule(self):
        bqm = dimod.BinaryQuadraticModel({0: -1, 1: 0.01}, {}, 0, 'BINARY')
        ground = {0: 1, 1: 0}
        state = hybrid.State.from_problem(bqm)

        pa = PopulationAnnealing(num_reads=1, num_iter=10, num_sweeps=10, beta_range=[0, 1000])
        ss = pa.run(state).result().samples

        self.assertDictEqual(ss.first.sample, ground)


@unittest.mock.patch(
    'hybrid.QPUSubproblemAutoEmbeddingSampler',
    lambda *a,**kw: QPUSubproblemAutoEmbeddingSampler(qpu_sampler=MockDWaveSampler())
)
class TestReferenceWorkflowsSmoke(unittest.TestCase):

    @parameterized.expand([
        (hybrid.ParallelTempering, dict(num_sweeps=10, num_replicas=2)),
        (hybrid.HybridizedParallelTempering, dict(num_sweeps=10, num_replicas=2)),
        (hybrid.PopulationAnnealing, dict(num_reads=10, num_iter=10, num_sweeps=10)),
        (hybrid.HybridizedPopulationAnnealing, dict(num_reads=10, num_iter=10, num_sweeps=10)),
        (hybrid.Kerberos, dict(sa_sweeps=10, tabu_timeout=10, qpu_sampler=MockDWaveSampler())),
        (hybrid.LatticeLNLS, dict(topology='cubic',qpu_sampler=MockDWaveSamplerGeneralization(topology_type='pegasus')),
         {'problem_dims' : (1,1,1)}), # 2x2x2 cubic over pegasus topology 
        (hybrid.LatticeLNLS, dict(topology='cubic',qpu_sampler=MockDWaveSamplerGeneralization(topology_type='chimera')),
         {'problem_dims' : (1,1,1)}), # 2x2x2 cubic over chimera topology 
        (hybrid.LatticeLNLS, dict(topology='pegasus',qpu_sampler=MockDWaveSamplerGeneralization(topology_type='pegasus')),{'problem_dims' : (3,1,1,2,4)}), #Single Pegasus Cell
        (hybrid.LatticeLNLS, dict(topology='chimera',qpu_sampler=MockDWaveSamplerGeneralization(topology_type='chimera')),{'problem_dims' : (2,2,2,4)}), #2x2 Chimera-Cell problem
        (hybrid.SimplifiedQbsolv, dict(max_iter=2)),
    ])
    def test_smoke(self, sampler_cls, sampler_params,state_params=None):
        
        if state_params is None:
            bqm = dimod.BinaryQuadraticModel.from_ising({}, {'ab': 1})
            state = hybrid.State.from_problem(bqm)
        else:
            from itertools import product
            geometric_labels = [range(dim) for dim in state_params['problem_dims']]
            var_names = product(*geometric_labels)
            bqm = dimod.BinaryQuadraticModel.from_ising({var : 0 for var in var_names}, {})
            bqm.linear[next(iter(bqm.linear))] = 1
            state_params['origin_embeddings'] = make_origin_embeddings(
                qpu_sampler=sampler_params['qpu_sampler'],
                lattice_type=sampler_params['topology'],
                problem_dims=state_params['problem_dims'],
                reject_small_problems=False)
            state = hybrid.State.from_problem(bqm,**state_params)
            
        w = sampler_cls(**sampler_params)
        ss = w.run(state).result().samples
        #The substitute for QPU (MockSampler) is not guaranteed
        #to return a minima, so this is a bit risky (change later).
        self.assertEqual(ss.first.energy, -1)
