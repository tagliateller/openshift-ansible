"""
Health checks for OpenShift clusters.
"""

import operator
import os

from abc import ABCMeta, abstractmethod, abstractproperty
from importlib import import_module

from ansible.module_utils import six
from ansible.module_utils.six.moves import reduce  # pylint: disable=import-error,redefined-builtin
from ansible.plugins.filter.core import to_bool as ansible_to_bool


class OpenShiftCheckException(Exception):
    """Raised when a check encounters a failure condition."""

    def __init__(self, name, msg=None):
        # msg is for the message the user will see when this is raised.
        # name is for test code to identify the error without looking at msg text.
        if msg is None:  # for parameter backward compatibility
            msg = name
            name = self.__class__.__name__
        self.name = name
        super(OpenShiftCheckException, self).__init__(msg)


class OpenShiftCheckExceptionList(OpenShiftCheckException):
    """A container for multiple logging errors that may be detected in one check."""
    def __init__(self, errors):
        self.errors = errors
        super(OpenShiftCheckExceptionList, self).__init__(
            'OpenShiftCheckExceptionList',
            '\n'.join(str(msg) for msg in errors)
        )

    # make iterable
    def __getitem__(self, index):
        return self.errors[index]


@six.add_metaclass(ABCMeta)
class OpenShiftCheck(object):
    """
    A base class for defining checks for an OpenShift cluster environment.

    Expect optional params: method execute_module, dict task_vars, and string tmp.
    execute_module is expected to have a signature compatible with _execute_module
    from ansible plugins/action/__init__.py, e.g.:
    def execute_module(module_name=None, module_args=None, tmp=None, task_vars=None, *args):
    This is stored so that it can be invoked in subclasses via check.execute_module("name", args)
    which provides the check's stored task_vars and tmp.
    """

    def __init__(self, execute_module=None, task_vars=None, tmp=None):
        self._execute_module = execute_module
        self.task_vars = task_vars or {}
        self.tmp = tmp

        # set to True when the check changes the host, for accurate total "changed" count
        self.changed = False

    @abstractproperty
    def name(self):
        """The name of this check, usually derived from the class name."""
        return "openshift_check"

    @property
    def tags(self):
        """A list of tags that this check satisfy.

        Tags are used to reference multiple checks with a single '@tagname'
        special check name.
        """
        return []

    @staticmethod
    def is_active():
        """Returns true if this check applies to the ansible-playbook run."""
        return True

    @abstractmethod
    def run(self):
        """Executes a check, normally implemented as a module."""
        return {}

    @classmethod
    def subclasses(cls):
        """Returns a generator of subclasses of this class and its subclasses."""
        # AUDIT: no-member makes sense due to this having a metaclass
        for subclass in cls.__subclasses__():  # pylint: disable=no-member
            yield subclass
            for subclass in subclass.subclasses():
                yield subclass

    def execute_module(self, module_name=None, module_args=None):
        """Invoke an Ansible module from a check.

        Invoke stored _execute_module, normally copied from the action
        plugin, with its params and the task_vars and tmp given at
        check initialization. No positional parameters beyond these
        are specified. If it's necessary to specify any of the other
        parameters to _execute_module then that should just be invoked
        directly (with awareness of changes in method signature per
        Ansible version).

        So e.g. check.execute_module("foo", dict(arg1=...))
        Return: result hash from module execution.
        """
        if self._execute_module is None:
            raise NotImplementedError(
                self.__class__.__name__ +
                " invoked execute_module without providing the method at initialization."
            )
        return self._execute_module(module_name, module_args, self.tmp, self.task_vars)

    def get_var(self, *keys, **kwargs):
        """Get deeply nested values from task_vars.

        Ansible task_vars structures are Python dicts, often mapping strings to
        other dicts. This helper makes it easier to get a nested value, raising
        OpenShiftCheckException when a key is not found.

        Keyword args:
          default:
            On missing key, return this as default value instead of raising exception.
          convert:
            Supply a function to apply to normalize the value before returning it.
            None is the default (return as-is).
            This function should raise ValueError if the user has provided a value
            that cannot be converted, or OpenShiftCheckException if some other
            problem needs to be described to the user.
        """
        if len(keys) == 1:
            keys = keys[0].split(".")

        try:
            value = reduce(operator.getitem, keys, self.task_vars)
        except (KeyError, TypeError):
            if "default" not in kwargs:
                raise OpenShiftCheckException(
                    "This check expects the '{}' inventory variable to be defined\n"
                    "in order to proceed, but it is undefined. There may be a bug\n"
                    "in Ansible, the checks, or their dependencies."
                    "".format(".".join(map(str, keys)))
                )
            value = kwargs["default"]

        convert = kwargs.get("convert", None)
        try:
            if convert is None:
                return value
            elif convert is bool:  # interpret bool as Ansible does, instead of python truthiness
                return ansible_to_bool(value)
            else:
                return convert(value)

        except ValueError as error:  # user error in specifying value
            raise OpenShiftCheckException(
                'Cannot convert inventory variable to expected type:\n'
                '  "{var}={value}"\n'
                '{error}'.format(var=".".join(keys), value=value, error=error)
            )

        except OpenShiftCheckException:  # some other check-specific problem
            raise

        except Exception as error:  # probably a bug in the function
            raise OpenShiftCheckException(
                'There is a bug in this check. While trying to convert variable \n'
                '  "{var}={value}"\n'
                'the given converter cannot be used or failed unexpectedly:\n'
                '{error}'.format(var=".".join(keys), value=value, error=error)
            )

    @staticmethod
    def get_major_minor_version(openshift_image_tag):
        """Parse and return the deployed version of OpenShift as a tuple."""
        if openshift_image_tag and openshift_image_tag[0] == 'v':
            openshift_image_tag = openshift_image_tag[1:]

        # map major release versions across releases
        # to a common major version
        openshift_major_release_version = {
            "1": "3",
        }

        components = openshift_image_tag.split(".")
        if not components or len(components) < 2:
            msg = "An invalid version of OpenShift was found for this host: {}"
            raise OpenShiftCheckException(msg.format(openshift_image_tag))

        if components[0] in openshift_major_release_version:
            components[0] = openshift_major_release_version[components[0]]

        components = tuple(int(x) for x in components[:2])
        return components

    def find_ansible_mount(self, path):
        """Return the mount point for path from ansible_mounts."""

        # reorganize list of mounts into dict by path
        mount_for_path = {
            mount['mount']: mount
            for mount
            in self.get_var('ansible_mounts')
        }

        # NOTE: including base cases '/' and '' to ensure the loop ends
        mount_targets = set(mount_for_path.keys()) | {'/', ''}
        mount_point = path
        while mount_point not in mount_targets:
            mount_point = os.path.dirname(mount_point)

        try:
            return mount_for_path[mount_point]
        except KeyError:
            known_mounts = ', '.join('"{}"'.format(mount) for mount in sorted(mount_for_path))
            raise OpenShiftCheckException(
                'Unable to determine mount point for path "{}".\n'
                'Known mount points: {}.'.format(path, known_mounts or 'none')
            )


LOADER_EXCLUDES = (
    "__init__.py",
    "mixins.py",
    "logging.py",
)


def load_checks(path=None, subpkg=""):
    """Dynamically import all check modules for the side effect of registering checks."""
    if path is None:
        path = os.path.dirname(__file__)

    modules = []

    for name in os.listdir(path):
        if os.path.isdir(os.path.join(path, name)):
            modules = modules + load_checks(os.path.join(path, name), subpkg + "." + name)
            continue

        if name.endswith(".py") and name not in LOADER_EXCLUDES:
            modules.append(import_module(__package__ + subpkg + "." + name[:-3]))

    return modules
