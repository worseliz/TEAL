# Copyright 2017 Battelle Energy Alliance, LLC
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
"""
  The Optimizer is a specialization of adaptive sampling.
  This base class defines the principle methods required for optimizers and provides some general utilities.

  Reworked 2020-01
  @author: talbpaul
"""
#for future compatibility with Python 3--------------------------------------------------------------
from __future__ import division, print_function, unicode_literals, absolute_import
#End compatibility block for Python 3----------------------------------------------------------------

#External Modules------------------------------------------------------------------------------------
import sys
import copy
import abc
import numpy as np
from collections import deque
#External Modules End--------------------------------------------------------------------------------

#Internal Modules------------------------------------------------------------------------------------
from utils import utils, randomUtils, InputData, InputTypes
import SupervisedLearning
from Samplers import AdaptiveSampler
#Internal Modules End--------------------------------------------------------------------------------

class Optimizer(AdaptiveSampler):
  """
    The Optimizer is a specialization of adaptive sampling.
    This base class defines the principle methods required for optimizers and provides some general utilities.
    This base class is responsible for:
     - Implementing Sampler API
     - Handling stochastic resampling
     - Establishing "trajectory" counter
     - Handling Constant, Function variables
     - Specifying objective variable
     - Assembling constraints
     - API for adding, removing trajectories
     - Prefix handling for trajectory, denoising
  """
  ##########################
  # Initialization Methods #
  ##########################
  @classmethod
  def getInputSpecification(cls):
    """
      Method to get a reference to a class that specifies the input data for class cls.
      @ In, cls, the class for which we are retrieving the specification
      @ Out, specs, InputData.ParameterInput, class to use for specifying input of cls.
    """
    specs = super(Optimizer, cls).getInputSpecification()
    # objective variable
    specs.addSub(InputData.parameterInputFactory('objective', contentType=InputTypes.StringType, strictMode=True))
    # modify Sampler variable nodes
    variable = specs.getSub('variable') # TODO use getter?
    #variable.removeSub('distribution')
    #variable.removeSub('grid')
    #variable.addSub(InputData.parameterInputFactory('lowerBound', contentType=InputTypes.FloatType)) # TODO quantity = 1
    #variable.addSub(InputData.parameterInputFactory('upperBound', contentType=InputTypes.FloatType)) # TODO quantity = 1
    variable.addSub(InputData.parameterInputFactory('initial', contentType=InputTypes.FloatListType)) # TODO quantity = 1
    # initialization
    ## TODO similar to MonteCarlo and other samplers, maybe overlap?
    init = InputData.parameterInputFactory('samplerInit', strictMode=True)
    minMaxEnum = InputTypes.makeEnumType('MinMax', 'MinMaxType', ['min', 'max'])
    seed = InputData.parameterInputFactory('initialSeed', contentType=InputTypes.IntegerType)
    minMax = InputData.parameterInputFactory('type', contentType=minMaxEnum)
    init.addSub(seed)
    init.addSub(minMax)
    specs.addSub(init)

    # TODO threshold, stochastic samples
    # assembled objects
    specs.addSub(InputData.assemblyInputFactory('TargetEvaluation', contentType=InputTypes.StringType, strictMode=True))
    specs.addSub(InputData.assemblyInputFactory('Constraint', contentType=InputTypes.StringType, strictMode=True))
    specs.addSub(InputData.assemblyInputFactory('Sampler', contentType=InputTypes.StringType, strictMode=True))
    return specs

  def __init__(self):
    """
      Constructor.
      @ In, None
      @ Out, None
    """
    AdaptiveSampler.__init__(self)
    self._seed = None
    self._minMax = None
    ## Instance Variable Initialization
    # public

    # _protected
    self._activeTraj = []      # tracks live trajectories
    self._numRepeatSamples = 1 # number of times to repeat sampling (e.g. denoising)
    self._objectiveVar = None  # objective variable for optimization

    # __private

    # additional methods
    self.addAssemblerObject('TargetEvaluation', '1') # Place where realization evaluations go
    self.addAssemblerObject('Constraint', '-1')      # Explicit (input-based) constraints
    self.addAssemblerObject('Sampler', '-1')         # This Sampler can be used to initialize the optimization initial points (e.g. partially replace the <initial> blocks for some variables)

    # register adaptive sample identification criteria
    self.registerIdentifier('traj') # the trajectory of interest

  def _localGenerateAssembler(self, initDict):
    """
      It is used for sending to the instanciated class, which is implementing the method, the objects that have been requested through "whatDoINeed" method
      Overloads the base Sampler class since optimizer has different requirements
      @ In, initDict, dict, dictionary ({'mainClassName(e.g., Databases):{specializedObjectName(e.g.,DatabaseForSystemCodeNamedWolf):ObjectInstance}'})
      @ Out, None
    """
    self.assemblerDict['Functions'    ] = []
    self.assemblerDict['Distributions'] = []
    self.assemblerDict['DataObjects'  ] = []
    for mainClass in ['Functions','Distributions','DataObjects']:
      for funct in initDict[mainClass]:
        self.assemblerDict[mainClass].append([mainClass,initDict[mainClass][funct].type,funct,initDict[mainClass][funct]])

  def localInputAndChecks(self, xmlNode, paramInput):
    """
      unfortunately-named method that serves as a pass-through for input reading.
      comes from inheriting from Sampler and _readMoreXML chain.
      @ In, xmlNode, xml.etree.ElementTree.Element, xml element node (don't use!)
      @ In, paramInput, InputData.ParameterInput, parameter specs interpreted
      @ Out, None
    """
    # this is just a passthrough until sampler gets reworked or renamed
    self.handleInput(paramInput)

  def _localWhatDoINeed(self):
    """
      Identifies needed distributions and functions.
      Overloads Sampler base implementation because of unique needs.
      @ In, None
      @ Out, needDict, dict, list of objects needed
    """
    needDict = {}
    needDict['Distributions'] = [(None,'all')]
    needDict['Functions'    ] = [(None,'all')]
    needDict['DataObjects'  ] = [(None,'all')]
    return needDict

  def handleInput(self, paramInput):
    """
      Read input specs
      @ In, paramInput, InputData.ParameterInput, parameter specs interpreted
      @ Out, None
    """
    # the reading of variables (dist or func) and constants already happened in _readMoreXMLbase in Sampler
    # objective var
    self._objectiveVar = paramInput.findFirst('objective')
    #
    # sampler init
    # self.readSamplerInit() can't be used because it requires the xml node
    init = paramInput.findFirst('samplerInit')
    if init is not None:
      # initialSeed
      seed = init.findFirst('seed')
      if seed is not None:
        self._seed = seed.value
      # minmax
      minMax = init.findFirst('type')
      if minMax is not None:
        self._minMax = minMax.value

  def initialize(self, externalSeeding=None, solutionExport=None):
    """
      This function should be called every time a clean optimizer is needed. Called before takeAstep in <Step>
      @ In, externalSeeding, int, optional, external seed
      @ In, solutionExport, DataObject, optional, a PointSet to hold the solution
      @ Out, None
    """
    # seed
    if self._seed is not None:
      randomUtils.randomSeed(self._seed)

  ###############
  # Run Methods #
  ###############
  def amIreadyToProvideAnInput(self):
    """
      This is a method that should be called from any user of the optimizer before requiring the generation of a new input.
      This method act as a "traffic light" for generating a new input.
      Reason for not being ready could be for example: exceeding number of model evaluation, convergence criteria met, etc.
      @ In, None
      @ Out, ready, bool, indicating the readiness of the optimizer to generate a new input.
    """
    TODO

  ###################
  # Utility Methods #
  ###################
  def checkConstraint(self, optVars):
    """
      Method to check whether a set of decision variables satisfy the constraint or not in UNNORMALIZED input space
      @ In, optVars, dict, dictionary containing the value of decision variables to be checked, in form of {varName: varValue}
      @ Out, satisfaction, tuple, (bool,list) => (variable indicating the satisfaction of constraints at the point optVars, masks for the under/over violations)
    """
    TODO

  @abc.abstractmethod
  def checkConvergence(self):
    """
      Method to check whether the convergence criteria has been met.
      @ In, none,
      @ Out, convergence, bool, variable indicating whether the convergence criteria has been met.
    """
    TODO

  def checkIfBetter(self, a, b):
    """
      Checks if a is preferable to b for this optimization problem.  Helps mitigate needing to keep
      track of whether a minimization or maximation problem is being run.
      @ In, a, float, value to be compared
      @ In, b, float, value to be compared against
      @ Out, checkIfBetter, bool, True if a is preferable to b for this optimization
    """
    if self.optType == 'min':
      return a <= b
    elif self.optType == 'max':
      return a >= b

  def _addTrackingInfo(self, info, **kwargs):
    """
      Creates realization identifiers to identifiy particular realizations as they return from the JobHandler.
      Expandable by inheritors.
      @ In, info, dict, dictionary of potentially-existing added identifiers
      @ In, kwargs, dict, dictionary of keyword arguments
      @ Out, None (but "info" gets modified)
    """
    # TODO shouldn't this require the realization and information to do right?
    info['traj'] = kwargs['traj']

  def denormalizeData(self, normalized):
    """
      Method to normalize the data
      @ In, normalized, dict, dictionary containing the value of decision variables to be deormalized, in form of {varName: varValue}
      @ Out, denormed, dict, dictionary containing the value of denormalized decision variables, in form of {varName: varValue}
    """
    TODO

  def normalizeData(self, denormed):
    """
      Method to normalize the data
      @ In, denormed, dict, dictionary containing the value of decision variables to be normalized, in form of {varName: varValue}
      @ Out, normalized, dict, dictionary containing the value of normalized decision variables, in form of {varName: varValue}
    """
    TODO
