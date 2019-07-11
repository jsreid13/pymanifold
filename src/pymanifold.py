from pprint import pprint
import json
import os
import sys
import networkx as nx
#  dReal SMT solver
from dreal.symbolic import Variable, logical_and
from dreal.api import CheckSatisfiability
from OMPython import ModelicaSystem

from src import constants, translate


class Fluid():
    """This is used to retrieve the parameters of common fluids used in
    microfluidics from constants.py so researchers can simply provide the fluid
    name as a parameter instead of several properties of the fluid
    """

    def __init__(self, fluid):
        """The properties stored in constants.py are retrieved here in the constructor
        """
        self.min_density = constants.FluidProperties().getDensity(fluid)
        self.min_resistivity = constants.FluidProperties().getResistivity(fluid)
        self.min_viscosity = constants.FluidProperties().getViscosity(fluid)
        self.min_pressure = False

        self.analyte_diffusivities = constants.FluidProperties().getDiffusivities(fluid)
        self.analyte_initial_concentrations = constants.FluidProperties().getInitialConcentrations(fluid)
        self.analyte_radii = constants.FluidProperties().getRadii(fluid)
        self.analyte_charges = constants.FluidProperties().getCharges(fluid)

    def updateFluidProperties(self,
                              min_density=False,
                              min_viscosity=False,
                              min_pressure=False,
                              min_resistivity=False
                              ):
        """If the user wants to tweek the values of the fluids manually, call this method
        TODO: Currently all parameters have to be provided, make it so only the ones
              provided are updated
        """
        self.min_density = min_density
        self.min_resistivity = min_resistivity
        self.min_viscosity = min_viscosity
        self.min_pressure = min_pressure

    def __repr__(self):
        """Representation of this object is all of the parameters together in a tuple
        """
        return repr((self.min_density, self.min_resistivity, self.min_viscosity, self.min_pressure))


class Schematic():
    """Create new schematic which contains all of the connections and ports
    within a microfluidic circuit to be solved for my an SMT solver to
    determine solvability of the circuit and the range of the parameters where
    it is still solvable
    """
    def __init__(self, dim):
        """Store the connections as a directed graph in NetworkX where each node
        is a point where fluid enters the channel or where two channels meet,
        information about each of the channels in a separate dictionary

        :param list dim: dimensions of the overall chip, [X_min, Y_min, X_max, X_min] (m)
        """
        self.exprs = []
        self.dim = dim

        # Add new node types and their validation method to this dict
        # to maintain consistent checking across all methods
        self.translation_strats = dir(translate)
        #  {'input': translate.translate_input,
        #                             'node': translate.translate_node,
        #                             'output': translate.translate_output,
        #                             't-junction': translate.translate_tjunc,
        #                             'rectangle': translate.translate_channel
        #                             }

        # DiGraph that will contain all nodes and channels
        self.dg = nx.DiGraph()

    def validate_params(self, params: dict, component: str, name: str):
        """Checks that the parameters provided to a primitive type definition are valid
        i.e. that strings are actually string, numbers are actually ints or floats

        :param params dict: Dictionary containing all parameters and their cooresponding type
        :param component str: What primitive type this is checking, Node, Port, Channel, etc.
        :param name str: Name of the component
        :raises: ValueError
        :returns: None

        """
        for param, value in params.items():
            # Parameter is still False, so skip it since user didnt define anything
            if not param:
                continue
            if value == 'string':
                if not isinstance(param, str):
                    raise TypeError("%s '%s' param %s must be a string" %
                                    (component, name, param))
            elif value == 'number':
                # list of values that should all be positive numbers, in doing so also
                # checks if its an int or float
                if not isinstance(param, int) and not isinstance(param, float):
                    raise TypeError("%s '%s' parameter '%s' must be int or float" %
                                    (component, name, param))
            elif value == 'negative number':
                # list of values that should all be positive numbers, in doing so also
                # checks if its an int or float
                try:
                    if param > 0:
                        raise ValueError("%s '%s' parameter '%s' must be >= 0" %
                                         (component, name, param))
                except TypeError as e:
                    raise TypeError("%s '%s' parameter '%s' must be int or float" %
                                    (component, name, param))
            elif value == 'positive number':
                # list of values that should all be positive numbers, in doing so also
                # checks if its an int or float
                try:
                    if param < 0:
                        raise ValueError("%s '%s' parameter '%s' must be >= 0" %
                                         (component, name, param))
                except TypeError as e:
                    raise TypeError("%s '%s' parameter '%s' must be int or float" %
                                    (component, name, param))

    def channel(self,
                port_from,
                port_to,
                min_length=False,
                min_width=False,
                min_height=False,
                min_resolution=False,
                kind='rectangle',
                phase='None',
                min_sampling_rate=1
                ):
        """Create new connection between two nodes/ports with attributes
        consisting of the dimensions of the channel to be used to create the
        SMT equations to calculate solvability of the circuit
        Units are in brackets

        :param str port_from: Port where fluid comes into the channel from
        :param str port_to: Port at the end of the channel where fluid exits
        :param float min_length: Constrain channel to be this long (m)
        :param float width: Constrain channel to be this wide (m)
        :param float height: Constrain channel to be this wide (m)
        :param str kind: Kind of cross section of the channel (rectangle)
        :param str phase: For channels connecting to a T-junction this must be
            either continuous, dispersed or output
        :returns: None -- no issues with creating this channel
        :raises: TypeError if an input parameter is wrong type
                 ValueError if an input parameter has an invalid value
        """
        # Collection of the kinds for which there are methods to calculate their
        # channel resistance
        valid_kinds = ("rectangle")

        name = (port_from, port_to)

        user_provided_params = {port_from: 'string',
                                port_to: 'string',
                                min_length: 'positive number',
                                min_width: 'positive number',
                                min_height: 'positive number',
                                min_resolution: 'positive number',
                                kind: 'string',
                                phase: 'string',
                                min_sampling_rate: 'positive number'
                                }
        # Checking that arguments are valid
        # TODO: Modify this to make it work for other channel shapes
        if kind not in valid_kinds:
            raise ValueError("Valid channel kinds are: %s" % valid_kinds)
        if kind == "rectangle":
            kind = "channel"

        self.validate_params(user_provided_params, 'Channel', name)

        if (port_from, port_to) in self.dg.edges:
            raise ValueError("Channel already exists between these nodes %s" % (port_from, port_to))
        if 'translate_' + kind.lower() not in self.translation_strats:
            raise ValueError("kind %s must be either %s" % ("translate_" + kind.lower(), self.translation_strats))

        # Add the information about that connection to another dict
        # There's extra parameters in here than in the arguments because they
        # are values calculated by later methods when creating the SMT eqns
        # Channels do not have pressure though, since it decreases linearly
        # across the channel
        attributes = {'kind': kind,
                      'length': Variable('_'.join([*name, 'length'])),
                      'min_length': min_length,
                      'width': Variable('_'.join([*name, 'width'])),
                      'min_width': min_width,
                      'height': Variable('_'.join([*name, 'height'])),
                      'min_height': min_height,
                      'resolution': Variable('_'.join([*name, 'resolution'])),
                      'min_resolution': min_resolution,
                      'flow_rate': Variable('_'.join([*name, 'flow_rate'])),
                      'droplet_volume': Variable('_'.join([*name, 'droplet_volume'])),
                      'viscosity': Variable('_'.join([*name, 'viscosity'])),
                      'resistance': Variable('_'.join([*name, 'resistance'])),
                      'phase': phase.lower(),
                      'port_from': port_from,
                      'port_to': port_to,
                      'x_detector': Variable('_'.join([*name, 'x_detector'])),
                      'min_sampling_rate': min_sampling_rate
                      }

        # If user provides values, put them into the attributes dictionary
        if not min_width:
            attributes['min_width'] = min_width
        if not min_length:
            attributes['min_length'] = min_length
        if not min_height:
            attributes['min_height'] = min_height

        # Create this edge in the graph
        self.dg.add_edge(*name)

        # Add argument to attributes within NetworkX
        for key, attr in attributes.items():
            self.dg.edges[port_from, port_to][key] = attr
        return

    def port(self,
             name,
             kind,
             min_pressure=False,
             min_flow_rate=False,
             x=False,
             y=False,
             fluid_name='default'
             ):
        """Create new port where fluids can enter or exit the circuit, any
        optional tag left empty will be converted to a variable for the SMT
        solver to solve for a give a value, units in brackets

        :param str name: The name of the port to use when defining channels
        :param str kind: Define if this is an 'input' or 'output' port
        :param float density: Density of fluid (kg/m^3)
        :param float min_viscosity: Viscosity of the fluid (Pa*s)
        :param float min_pressure: Pressure of the input fluid, (Pa)
        :param float min_flow_rate - flow rate of input fluid, (m^3/s)
        :param float X: x-position of port on chip schematic (m)
        :param float Y: y-position of port on chip schematic (m)
        :returns: None -- no issues with creating this port
        :raises: TypeError if an input parameter is wrong type
                 ValueError if an input parameter has an invalid value
        """
        user_provided_params = {name: 'string',
                                min_pressure: 'positive number',
                                min_flow_rate: 'positive number',
                                x: 'positive number',
                                y: 'positive number',
                                kind: 'string',
                                fluid_name: 'string'
                                }
        # Checking that arguments are valid
        self.validate_params(user_provided_params, 'port', name)

        if name in self.dg.nodes:
            raise ValueError("Must provide a unique name")
        if 'translate_' + kind.lower() not in self.translation_strats:
            raise ValueError("kind %s must be either %s" % ("translate_" + kind.lower(), self.translation_strats))

        # Initialize fluid properties
        fluid_properties = Fluid(fluid_name)

        # Ports are stored with nodes because ports are just a specific type of
        # node that has a constant flow rate
        # only accept ports of the right kind (input or output)
        attributes = {'kind': kind.lower(),
                      'viscosity': Variable(name + '_viscosity'),
                      'min_viscosity': fluid_properties.min_viscosity,
                      'pressure': Variable(name + '_pressure'),
                      'min_pressure': min_pressure,
                      'flow_rate': Variable(name + '_flow_rate'),
                      'min_flow_rate': min_flow_rate,
                      'density': Variable(name + '_density'),
                      'min_density': fluid_properties.min_density,
                      'x': Variable(name + '_x'),
                      'y': Variable(name + '_y'),
                      'min_x': x,
                      'min_y': y,
                      'analyte_diffusivities': fluid_properties.analyte_diffusivities,
                      'analyte_initial_concentrations': fluid_properties.analyte_initial_concentrations,
                      'analyte_radii': fluid_properties.analyte_radii,
                      'analyte_charges': fluid_properties.analyte_charges
                      }

        # If user provides values, put them into the attributes dictionary
        if not x:
            attributes['min_x'] = x
        if not y:
            attributes['min_y'] = y
        if not min_flow_rate:
            attributes['min_flow_rate'] = min_flow_rate
        if not min_pressure:
            attributes['min_pressure'] = min_pressure

        # Create this node in the graph
        self.dg.add_node(name)
        # Add argument to attributes within NetworkX
        for key, attr in attributes.items():
            self.dg.nodes[name][key] = attr
        return

    def node(self, name, x=False, y=False, kind='node', c=0.4, p=0.5, qf=0.9):
        # TODO: Add ability to add features when this is  tjunc, same for channel
        """Create new node where fluids merge or split, kind of node (T-junction,
        Y-junction, cross, etc.) can be specified if not then a basic node
        connecting multiple channels will be created, units in brackets

        :param str name: Name of the node to use when connecting to a channel
        :param float x:  Set the X position of this node (m)
        :param float y:  Set the Y position of this node (m)
        :param str kind: The type of node this is, default is node, other
            option is t-junction
        :returns: None -- no issues with creating this node
        :raises: TypeError if an input parameter is wrong type
                 ValueError if an input parameter has an invalid value
        """
        user_provided_params = {name: 'string',
                                x: 'positive number',
                                y: 'positive number',
                                kind: 'string',
                                c: 'positive number',
                                p: 'positive number',
                                qf: 'positive number'
                                }
        # Checking that arguments are valid
        self.validate_params(user_provided_params, 'node', name)

        if name in self.dg.nodes:
            raise ValueError("Must provide a unique name")
        if 'translate_' + kind.lower() not in self.translation_strats:
            raise ValueError("kind %s must be either %s" % ("translate_" + kind.lower(), self.translation_strats))

        # Ports are stored with nodes because ports are just a specific type of
        # node that has a constant flow rate only accept ports of the right
        # kind (input or output)
        # While the user can't define most parameters for a node because it
        # doesnt take an input from outside the chip, they're still added
        # and set to zero so checks to each node to see if there is a min
        # value for each node doesn't raise a KeyError
        attributes = {'kind': kind.lower(),
                      'pressure': Variable(name + '_pressure'),
                      'min_pressure': None,
                      'flow_rate': Variable(name + '_flow_rate'),
                      'min_flow_rate': None,
                      'viscosity': Variable(name + '_viscosity'),
                      'min_viscosity': None,
                      'density': Variable(name + '_density'),
                      'min_density': None,
                      'x': Variable(name + '_x'),
                      'min_x': None,
                      'y': Variable(name + '_y'),
                      'min_y': None,
                      'c': c,
                      'p': p,
                      'qf': qf
                      }

        # If user provides values, put them into the attributes dictionary
        if x:
            attributes['min_x'] = x
        if y:
            attributes['min_y'] = y

        # Create this node in the graph
        self.dg.add_node(name)
        # Add argument to attributes within NetworkX
        for key, attr in attributes.items():
                self.dg.nodes[name][key] = attr
        return

    def elec_port(self,
                  name,
                  kind,
                  min_pressure=False,
                  min_flow_rate=False,
                  x=False,
                  y=False,
                  voltage=False,
                  current=False,
                  fluid_name='default'):
        """Create new electrical port where fluids and voltages can enter or exit the circuit, any
        optional tag left empty will be converted to a variable for the SMT
        solver to solve for a given value, units in brackets

        :param str name: The name of the port to use when defining channels
        :param str kind: Define if this is an 'input' or 'output' port
        :param float density: Density of fluid (kg/m^3)
        :param float min_viscosity: Viscosity of the fluid (Pa*s)
        :param float min_pressure: Pressure of the input fluid, (Pa)
        :param float min_flow_rate - flow rate of input fluid, (m^3/s)
        :param float X: x-position of port on chip schematic (m)
        :param float Y: y-position of port on chip schematic (m)
        :param float voltage: Voltage value passing through the port (V)
        :param float current: Current value passing through the port (A)
        :returns: None -- no issues with creating this port
        :raises: TypeError if an input parameter is wrong type
                 ValueError if an input parameter has an invalid value
        """
        user_provided_params = {name: 'string',
                                min_pressure: 'positive number',
                                min_flow_rate: 'positive number',
                                x: 'positive number',
                                y: 'positive number',
                                voltage: 'number',
                                current: 'positive number',
                                kind: 'string',
                                fluid_name: 'string'
                                }
        # Checking that arguments are valid
        self.validate_params(user_provided_params, 'electrical port', name)

        if name in self.dg.nodes:
            raise ValueError("Must provide a unique name")
        if "translate_" + kind.lower() not in self.translation_strats:
            raise ValueError("kind %s must be either %s" % ("translate_" + kind.lower(), self.translation_strats))

        # Initialize fluid properties
        fluid_properties = Fluid(fluid_name)

        # Ports are stored with nodes because ports are just a specific type of
        # node that has a constant flow rate
        # only accept ports of the right kind (input or output)
        attributes = {'kind': kind.lower(),
                      'viscosity': Variable(name + '_viscosity'),
                      'min_viscosity': fluid_properties.min_viscosity,
                      'pressure': Variable(name + '_pressure'),
                      'min_pressure': min_pressure,
                      'flow_rate': Variable(name + '_flow_rate'),
                      'min_flow_rate': min_flow_rate,
                      'density': Variable(name + '_density'),
                      'min_density': fluid_properties.min_density,
                      'x': Variable(name + '_X'),
                      'y': Variable(name + '_Y'),
                      'min_x': x,
                      'min_y': y,
                      'voltage': voltage,
                      'current': current,
                      }

        # Create this node in the graph
        self.dg.add_node(name)
        # Add argument to attributes within NetworkX
        for key, attr in attributes.items():
            self.dg.nodes[name][key] = attr
        return

    def translate_schematic(self):
        """Validates that each node has the correct input and output
        conditions met then translates it into SMT solver syntax
        Generates SMT formulas to simulate specialized nodes like T-junctions
        and stores them in self.exprs
        """
        # if schematic has no input then it is invalid
        has_input = False

        # The translate method names are stored in a dictionary name where
        # the key is the kind of that node and Call on all input nodes and it
        # will recursive traverse the circuit
        for name in self.dg.nodes:
            kind = self.dg.nodes[name]['kind']
            if kind == 'input':
                has_input = True
                # first ensure that it has an output
                has_output = False
                # TODO: Need to create list of output + input nodes to see if they connect
                for x, y in self.dg.nodes(data=True):
                    if y['kind'] == 'output':
                        # There's an output, so call translate on input
                        has_output = True
                # TODO: Output may not be connected to input, check for it
                if has_output:
                    [self.exprs.append(val) for val in translate.translate_input(self.dg, name)]
                else:
                    raise ValueError('Schematic input %s has no output' % name)
        if not has_input:
            raise ValueError('Schematic has no input')

        # finish by constraining nodes to be within chip area
        for name in self.dg.nodes:
            [self.exprs.append(val) for val in translate.translate_chip(self.dg, name, self.dim)]
        return

    def invoke_backend(self, _show):
        """Combine all of the SMT expressions into one expression to sent to dReal
        solver to determine solvability

        :param bool show: If true then the full SMT formula that was created is
                          printed
        :returns: dReal model showing the values for each of the parameters
        """
        formula = logical_and(*self.exprs)
        # Prints the generated formula in full, remove serialize for shortened
        if _show:
            #  nx.draw(self.dg)
            #  plt.show()
            print(formula)
        # Return None if not solvable, returns a dict-like structure giving the
        # range of values for each Variable
        model = CheckSatisfiability(formula, 10)
        if model:
            return model
        else:
            return "No solution found"

    def solve(self, show=False):
        """Create the SMT2 equation for this schematic outlining the design
        of a microfluidic circuit and use dReal to solve it

        :param bool show: If true then the full SMT formula that was created is
                          printed
        :returns: dReal model showing the values for each of the parameters
        """
        self.translate_schematic()
        return self.invoke_backend(show)

    def to_json(self, path=os.getcwd() + 'test.json'):
        """Converts designed schematic to a json file following Manifold's intermediate
        representation syntax to work with other parts of Manifold if needed

        :param str path: Path to save the json file to on the computer
        """
        nx_json = nx.readwrite.json_graph.node_link_data(self.dg)
        output = self.solve()
        dreal_output = {}
        for name, interval in output.items():
            value = (interval.lb(), interval.ub())
            if sys.float_info.max in value:
                print('Warning: %s range includes inf, needs upper bound' % name)
            dreal_output[name] = value

        for attribute, value in dreal_output.items():
            attr_split = str(attribute).split("_")
            for link_attribute_dict in nx_json["links"]:
                if link_attribute_dict["source"] == attr_split[0] and\
                        link_attribute_dict["target"] == attr_split[1]:
                    link_attribute_dict["_".join(attr_split[2:])] = value

            for node_attribute_dict in nx_json["nodes"]:
                if node_attribute_dict["id"] == attr_split[0]:
                    node_attribute_dict["_".join(attr_split[1:])] = value

        manifold_ir = {"name": "Json Data",
                       "userDefinedTypes": {},
                       "portTypes": {},
                       "nodeTypes": {},
                       "constraintTypes": {},
                       "nodes": {},
                       "connections": {},
                       "constraints": {}
                       }
        for key, value in nx_json.items():
            if type(value) != list:
                manifold_ir['constraints'][key] = value

        for idx, link_attribute_dict in enumerate(nx_json["links"]):
            # Channel name is ch1, ch2, etc.
            channel_id = "ch" + str(idx)
            # Source is the same as port_from, but generated by Networkx
            manifold_ir["connections"][channel_id] = {"from": link_attribute_dict["source"],
                                                      "to": link_attribute_dict["target"],
                                                      "attributes": {}
                                                      }
            for key, value in link_attribute_dict.items():
                # These are accounted for above
                if key not in ("port_from", "port_to", "source", "target") and\
                        not isinstance(value, Variable):
                    manifold_ir["connections"][channel_id]["attributes"][key] = value

        for idx, node_attribute_dict in enumerate(nx_json["nodes"]):
            # Node name is pT1, pT2, etc.
            node_id = "pT" + str(idx)
            # Kind is used to determine if node is a port
            node_kind = node_attribute_dict["kind"]
            manifold_ir["nodes"][node_id] = {"type": node_kind,
                                             "portAttrs": node_attribute_dict["id"],
                                             "attributes": {}
                                             }
            # If the node kind is input or output then it is a port
            if node_kind in ("input", "output"):
                manifold_ir["portTypes"][node_id] = {"signalType": node_attribute_dict["kind"],
                                                     "attributes": {}
                                                     }
            else:
                manifold_ir["nodeTypes"][node_id] = {"signalType": node_attribute_dict["kind"],
                                                     "attributes": {}
                                                     }
            # Dump values of all other parameters into that entry for node, and portTypes if its
            # a port, nodeTypes if its just a node
            for key, value in node_attribute_dict.items():
                if isinstance(value, Variable):
                    continue
                manifold_ir["nodes"][node_id]["attributes"][key] = value
                if node_kind in ("input", "output"):
                    manifold_ir["portTypes"][node_id]["attributes"][key] = value
                else:
                    manifold_ir["nodeTypes"][node_id]["attributes"][key] = value
        pprint(dreal_output)
        pprint(manifold_ir)

        with open(path, 'w') as outfile:
            json.dump(manifold_ir, outfile, separators=(',', ':'))

    def to_modelica(self):
        """Convert the schematic to a valid Modelica file
        :returns: None
        """
        mod = ModelicaSystem("TJunctionSingleDrop.mo",
                             "TJunctionSingleDrop",
                             ["Modelica"]
                             )
        return mod.getQuantities()


if __name__ == '__main__':
    sch = Schematic([0, 0, 1, 1])
    output = sch.to_modelica()
    print(output)
