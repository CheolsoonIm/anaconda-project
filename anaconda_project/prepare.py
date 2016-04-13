# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
"""Prepare a project to run."""
from __future__ import absolute_import
from __future__ import print_function

from abc import ABCMeta, abstractmethod
import os
import subprocess
import sys
from copy import copy, deepcopy

from anaconda_project.internal.metaclass import with_metaclass
from anaconda_project.internal import prepare_ui
from anaconda_project.internal.toposort import toposort_from_dependency_info
from anaconda_project.local_state_file import LocalStateFile
from anaconda_project.provide import (_all_provide_modes, PROVIDE_MODE_DEVELOPMENT)
from anaconda_project.plugins.provider import ProvideContext
from anaconda_project.plugins.requirement import EnvVarRequirement


def _update_environ(dest, src):
    """Overwrite ``environ`` with any additions from the prepared environ.

    Does not remove any variables from ``environ``.
    """
    # updating os.environ can be a memory leak, so we only update
    # those values that actually changed.
    for key, value in src.items():
        if key not in dest or dest[key] != value:
            dest[key] = value


class PrepareResult(with_metaclass(ABCMeta)):
    """Abstract class describing the result of preparing the project to run."""

    def __init__(self, logs, statuses):
        """Construct an abstract PrepareResult."""
        self._logs = logs
        self._statuses = tuple(statuses)

    def __bool__(self):
        """True if we were successful."""
        return not self.failed

    def __nonzero__(self):
        """True if we were successful."""
        return self.__bool__()  # pragma: no cover (py2 only)

    @property
    @abstractmethod
    def failed(self):
        """True if we failed to do what this stage was intended to do.

        If ``execute()`` returned non-None, the failure may not be fatal; stages
        can continue to be executed and may resolve the issue.
        """
        pass  # pragma: no cover

    @property
    def logs(self):
        """Get lines of debug log output.

        Does not include errors in case of failure. This is the
        "stdout" logs only.
        """
        return self._logs

    def print_output(self):
        """Print logs and errors to stdout and stderr."""
        for log in self.logs:
            print(log, file=sys.stdout)
            # be sure we print all these before the errors
            sys.stdout.flush()

    @property
    def statuses(self):
        """Get latest RequirementStatus if available.

        If we failed before we even checked statuses, this will be an empty list.
        """
        return self._statuses

    def status_for_env_var(self, env_var):
        """Get status for the given env var, or None if unknown."""
        for status in self.statuses:
            if isinstance(status.requirement, EnvVarRequirement) and status.requirement.env_var == env_var:
                return status
        return None


class PrepareSuccess(PrepareResult):
    """Class describing the successful result of preparing the project to run."""

    def __init__(self, logs, statuses, command_exec_info, environ):
        """Construct a PrepareSuccess indicating a successful prepare stage."""
        super(PrepareSuccess, self).__init__(logs, statuses)
        self._command_exec_info = command_exec_info
        self._environ = environ

    @property
    def failed(self):
        """Get False for PrepareSuccess."""
        return False

    @property
    def command_exec_info(self):
        """``CommandExecInfo`` instance if available, None if not."""
        return self._command_exec_info

    @property
    def environ(self):
        """Computed environment variables for the project."""
        return self._environ

    def update_environ(self, environ):
        """Overwrite ``environ`` with any additions from the prepared environ.

        Does not remove any variables from ``environ``.
        """
        _update_environ(environ, self._environ)


class PrepareFailure(PrepareResult):
    """Class describing the failed result of preparing the project to run."""

    def __init__(self, logs, statuses, errors):
        """Construct a PrepareFailure indicating a failed prepare stage."""
        super(PrepareFailure, self).__init__(logs, statuses)
        self._errors = errors

    @property
    def failed(self):
        """Get True for PrepareFailure."""
        return True

    @property
    def errors(self):
        """Get lines of error output."""
        return self._errors

    def print_output(self):
        """Override superclass to also print errors."""
        super(PrepareFailure, self).print_output()
        for error in self.errors:
            print(error, file=sys.stderr)


class ConfigurePrepareContext(object):
    """Information needed to configure a stage."""

    def __init__(self, environ, local_state_file, statuses):
        """Construct a ConfigurePrepareContext."""
        self.environ = environ
        self.local_state_file = local_state_file
        self.statuses = statuses
        if len(statuses) > 0:
            from anaconda_project.plugins.requirement import RequirementStatus
            assert isinstance(statuses[0], RequirementStatus)


class PrepareStage(with_metaclass(ABCMeta)):
    """A step in the project preparation process."""

    @property
    @abstractmethod
    def description_of_action(self):
        """Get a user-visible description of what happens if this step is executed."""
        pass  # pragma: no cover

    @property
    @abstractmethod
    def failed(self):
        """Synonym for result.failed, only available after ``execute()``."""
        pass  # pragma: no cover

    @abstractmethod
    def configure(self):
        """Get a ``ConfigurePrepareContext`` or None if no configuration is needed.

        Configuration should be done before execute().

        Returns:
          a ``ConfigurePrepareContext`` or None
        """
        pass  # pragma: no cover

    @abstractmethod
    def execute(self):
        """Run this step and return a new stage, or None if we are done or failed."""
        pass  # pragma: no cover

    @property
    @abstractmethod
    def result(self):
        """The ``PrepareResult`` (only available if ``execute()`` has been called)."""
        pass  # pragma: no cover

    @property
    @abstractmethod
    def statuses_before_execute(self):
        """``RequirementStatus`` list before execution.

        This list includes all known requirements and their statuses, while the list
        in the ``configure()`` context only includes those that should be configured
        prior to this stage's execution.
        """
        pass  # pragma: no cover

    @property
    @abstractmethod
    def statuses_after_execute(self):
        """``RequirementStatus`` list after execution.

        This list includes all known requirements and their statuses, as changed
        by ``execute()``. This property cannot be read prior to ``execute()``.
        """
        pass  # pragma: no cover


# This is defined to keep the same requirements from old_statuses
# in the refreshed list, even if they are missing from
# rechecked_statuses, and it does not add any new requirements
# from rechecked_statuses to the refreshed list.
def _refresh_status_list(old_statuses, rechecked_statuses):
    new_by_req = dict()
    for status in rechecked_statuses:
        new_by_req[status.requirement] = status
    updated = []
    for status in old_statuses:
        updated.append(new_by_req.get(status.requirement, status))
    return updated


class _FunctionPrepareStage(PrepareStage):
    """A stage chain where the description and the execute function are passed in to the constructor."""

    def __init__(self, description, statuses, execute, config_context=None):
        # the execute function is supposed to set these two (via accessor)
        self._result = None
        self._statuses_after_execute = None

        self._description = description
        self._statuses_before_execute = statuses
        self._execute = execute
        self._config_context = config_context

    # def __repr__(self):
    #    return "_FunctionPrepareStage(%r)" % (self._description)

    @property
    def description_of_action(self):
        return self._description

    @property
    def failed(self):
        return self.result.failed

    def configure(self):
        return self._config_context

    def execute(self):
        return self._execute(self)

    @property
    def result(self):
        if self._result is None:
            raise RuntimeError("result property isn't available until after execute()")
        return self._result

    @property
    def statuses_before_execute(self):
        return self._statuses_before_execute

    @property
    def statuses_after_execute(self):
        if self._statuses_after_execute is None:
            raise RuntimeError("statuses_after_execute isn't available until after execute()")
        return self._statuses_after_execute

    def set_result(self, result, rechecked_statuses):
        assert result is not None
        self._statuses_after_execute = _refresh_status_list(self._statuses_before_execute, rechecked_statuses)
        self._result = result


class _AndThenPrepareStage(PrepareStage):
    """A stage chain which runs an ``and_then`` function after it executes successfully."""

    def __init__(self, stage, and_then):
        self._stage = stage
        self._and_then = and_then

    # def __repr__(self):
    #    return "_AndThenPrepareStage(%r, %r)" % (self._stage, self._and_then)

    @property
    def description_of_action(self):
        return self._stage.description_of_action

    @property
    def failed(self):
        return self._stage.failed

    def configure(self):
        return self._stage.configure()

    def execute(self):
        next = self._stage.execute()
        if next is None:
            if self._stage.failed:
                return None
            else:
                return self._and_then(self._stage.statuses_after_execute)
        else:
            return _AndThenPrepareStage(next, self._and_then)

    @property
    def result(self):
        return self._stage.result

    @property
    def statuses_before_execute(self):
        return self._stage.statuses_before_execute

    @property
    def statuses_after_execute(self):
        return self._stage.statuses_after_execute


def _after_stage_success(stage, and_then):
    """Run and_then function after stage executes successfully.

    and_then may return another stage, or None. It takes
    the current list of updated statuses as a parameter.
    """
    assert stage is not None
    return _AndThenPrepareStage(stage, and_then)


def _sort_statuses(environ, local_state, statuses, missing_vars_getter):
    def get_node_key(status):
        # If we add a Requirement that isn't an EnvVarRequirement,
        # we can simply return the requirement object here as its
        # own key I believe. But for now that doesn't happen.
        assert hasattr(status.requirement, 'env_var')
        return status.requirement.env_var

    def get_dependency_keys(status):
        config_keys = set()
        for env_var in missing_vars_getter(status):
            config_keys.add(env_var)
        return config_keys

    def can_ignore_dependency_on_key(key):
        # if a key is already in the environment, we don't have to
        # worry about it existing as a node in the graph we are
        # toposorting
        return key in environ

    return toposort_from_dependency_info(statuses, get_node_key, get_dependency_keys, can_ignore_dependency_on_key)


def _configure_and_provide(project, environ, local_state, statuses, all_statuses, keep_going_until_success, mode,
                           provide_whitelist, extra_command_args):
    def provide_stage(stage):
        def get_missing_to_provide(status):
            return status.analysis.missing_env_vars_to_provide

        sorted = _sort_statuses(environ, local_state, statuses, get_missing_to_provide)

        # we have to recheck all the statuses in case configuration happened
        rechecked = []
        for status in sorted:
            rechecked.append(status.recheck(environ, local_state))

        logs = []
        errors = []
        did_any_providing = False

        for status in rechecked:
            if provide_whitelist is not None and \
               isinstance(status.requirement, EnvVarRequirement) and \
               status.requirement.env_var not in provide_whitelist:
                continue
            elif status.has_been_provided:
                continue
            else:
                did_any_providing = True
                context = ProvideContext(environ, local_state, status, mode)
                status.provider.provide(status.requirement, context)
                logs.extend(context.logs)
                errors.extend(context.errors)

        if did_any_providing:
            old = rechecked
            rechecked = []
            for status in old:
                rechecked.append(status.recheck(environ, local_state))

        failed = False
        for status in rechecked:
            if not status:
                errors.append("missing requirement to run this project: {requirement.title}"
                              .format(requirement=status.requirement))
                errors.append("  {why_not}".format(why_not=status.status_description))
                failed = True

        result_statuses = _refresh_status_list(all_statuses, rechecked)
        if failed:
            stage.set_result(PrepareFailure(logs=logs, statuses=result_statuses, errors=errors), rechecked)
            if keep_going_until_success:
                return _start_over(stage.statuses_after_execute, rechecked)
            else:
                return None
        else:
            exec_info = project.exec_info_for_environment(environ, extra_command_args)
            stage.set_result(
                PrepareSuccess(logs=logs,
                               statuses=result_statuses,
                               command_exec_info=exec_info,
                               environ=environ),
                rechecked)
            return None

    def _start_over(updated_all_statuses, updated_statuses):
        configure_context = ConfigurePrepareContext(environ=environ,
                                                    local_state_file=local_state,
                                                    statuses=updated_statuses)
        return _FunctionPrepareStage("Set up project.", updated_all_statuses, provide_stage, configure_context)

    return _start_over(all_statuses, statuses)


def _partition_first_group_to_configure(environ, local_state, statuses):
    def get_missing_to_configure(status):
        return status.analysis.missing_env_vars_to_configure

    sorted = _sort_statuses(environ, local_state, statuses, get_missing_to_configure)

    # We want "head" to be everything up to but not including the
    # first requirement that's missing needed env vars. head
    # should then include the requirement that supplies the
    # missing env var.

    head = []
    tail = []

    sorted.reverse()  # to efficiently pop
    while sorted:
        status = sorted.pop()

        missing_vars = status.provider.missing_env_vars_to_configure(status.requirement, environ, local_state)

        if len(missing_vars) > 0:
            tail.append(status)
            break
        else:
            head.append(status)

    sorted.reverse()  # put it back in order
    tail.extend(sorted)

    return (head, tail)


def _process_requirement_statuses(project, environ, local_state, current_statuses, all_statuses,
                                  keep_going_until_success, mode, provide_whitelist, extra_command_args):
    (initial, remaining) = _partition_first_group_to_configure(environ, local_state, current_statuses)

    # a surprising thing here is that the "stages" from
    # _configure_and_provide() can be user-visible, so we always
    # want at least one (even when all the lists are empty), but
    # we don't want to pointlessly create two by splitting off an
    # empty list when we already have a list. So we want two
    # _configure_and_provide() only if there are two non-empty lists,
    # but we always want at least one _configure_and_provide()

    def _stages_for(statuses):
        return _configure_and_provide(project, environ, local_state, statuses, all_statuses, keep_going_until_success,
                                      mode, provide_whitelist, extra_command_args)

    if len(initial) > 0 and len(remaining) > 0:

        def process_remaining(updated_all_statuses):
            # get the new status for each remaining requirement
            updated = _refresh_status_list(remaining, updated_all_statuses)
            return _process_requirement_statuses(project, environ, local_state, updated, updated_all_statuses,
                                                 keep_going_until_success, mode, provide_whitelist, extra_command_args)

        return _after_stage_success(_stages_for(initial), process_remaining)
    elif len(initial) > 0:
        return _stages_for(initial)
    else:
        return _stages_for(remaining)


def _add_missing_env_var_requirements(project, environ, local_state, statuses):
    by_env_var = dict()
    for status in statuses:
        # if we add requirements with no env_var, change this to
        # skip those requirements here
        assert hasattr(status.requirement, 'env_var')
        by_env_var[status.requirement.env_var] = status.requirement

    needed_env_vars = set()
    for status in statuses:
        needed_env_vars.update(status.provider.missing_env_vars_to_configure(status.requirement, environ, local_state))
        needed_env_vars.update(status.provider.missing_env_vars_to_provide(status.requirement, environ, local_state))

    created_anything = False

    for env_var in needed_env_vars:
        if env_var not in by_env_var:
            created_anything = True
            requirement = project.plugin_registry.find_requirement_by_env_var(env_var, options=dict())
            statuses.append(requirement.check_status(environ, local_state))

    if created_anything:
        # run the whole above again to find any transitive requirements of the new providers
        _add_missing_env_var_requirements(project, environ, local_state, statuses)


def _first_stage(project, environ, local_state, statuses, keep_going_until_success, mode, provide_whitelist,
                 extra_command_args):
    assert 'PROJECT_DIR' in environ

    _add_missing_env_var_requirements(project, environ, local_state, statuses)

    first_stage = _process_requirement_statuses(project, environ, local_state, statuses, statuses,
                                                keep_going_until_success, mode, provide_whitelist, extra_command_args)

    return first_stage


def prepare_in_stages(project,
                      environ=None,
                      keep_going_until_success=False,
                      mode=PROVIDE_MODE_DEVELOPMENT,
                      provide_whitelist=None,
                      extra_command_args=None):
    """Get a chain of all steps needed to get a project ready to execute.

    This function does not immediately do anything; it returns a
    ``PrepareStage`` object which can be executed. Executing each
    stage may return a new stage, or may return ``None``. If a
    stage returns ``None``, preparation is done and the ``failed``
    property of the stage indicates whether it failed.

    Executing a stage may ask the user questions, may start
    services, run scripts, load configuration, install
    packages... it can do anything. Expect side effects.

    Args:
        project (Project): the project
        environ (dict): the environment to start from (None to use os.environ)
        keep_going_until_success (bool): keep returning new stages until all requirements are met
        mode (str): One of ``PROVIDE_MODE_PRODUCTION``, ``PROVIDE_MODE_DEVELOPMENT``, ``PROVIDE_MODE_CHECK``
        provide_whitelist (iterable of str): ONLY call provide() for the listed env vars' requirements
        extra_command_args (list of str): extra args for the command we prepare

    Returns:
        The first ``PrepareStage`` in the chain of steps.

    """
    if mode not in _all_provide_modes:
        raise ValueError("invalid provide mode " + mode)

    assert not project.problems

    if environ is None:
        environ = os.environ

    assert 'PATH' in environ

    # we modify a copy, which 1) makes all our changes atomic and
    # 2) minimizes memory leaks on systems that use putenv().
    #
    # On Linux, it appears we must use deepcopy (vs plain copy) or
    # we still modify os.environ somehow.
    #
    # On the Windows CI server, but NOT on my local Windows 10
    # machine, deepcopy() didn't work but adding the extra .copy()
    # fixed it. The failure mode was that changes to PATH in the
    # copy were not visible via os.environ or os.getenv('PATH'),
    # but they DID affect what subprocess.Popen was able to see,
    # so that a test which modified PATH in this environ_copy
    # would break subsequent tests (such as test_conda_api which
    # tries to run conda). Anyway, presumably this is a bug in
    # something, but I'm not sure in what. If you can remove the
    # extra copy() and still pass all tests on all platforms at
    # some point in the future, feel free to clean up this
    # hackery.
    environ_copy = deepcopy(environ.copy())

    # many requirements and providers might need this, plus
    # it's useful for scripts to find their source tree.
    environ_copy['PROJECT_DIR'] = project.directory_path

    local_state = LocalStateFile.load_for_directory(project.directory_path)

    statuses = []
    for requirement in project.requirements:
        status = requirement.check_status(environ_copy, local_state)
        statuses.append(status)

    return _first_stage(project, environ_copy, local_state, statuses, keep_going_until_success, mode, provide_whitelist,
                        extra_command_args)


def _project_problems_to_prepare_failure(project):
    if project.problems:
        errors = []
        errors.append("Unable to load project:")
        for problem in project.problems:
            errors.append("  %s" % problem)
        return PrepareFailure(logs=[], statuses=(), errors=errors)
    else:
        return None


def prepare_without_interaction(project,
                                environ=None,
                                mode=PROVIDE_MODE_DEVELOPMENT,
                                provide_whitelist=None,
                                extra_command_args=None):
    """Prepare a project to run one of its commands.

    This method doesn't ask the user any questions, so the
    ``provide_mode`` lets you specify defaults suitable for a
    local workstation, a production deployment, or "check only"
    defaults ("check only" means don't do anything just check
    status).

    This method returns a result object. The result object has
    a ``failed`` property.  If the result is failed, the
    ``errors`` property has the errors.  If the result is not
    failed, the ``command_exec_info`` property has the stuff
    you need to run the project's default command, and the
    ``environ`` property has the updated environment. The
    passed-in ``environ`` is not modified in-place.

    You can update your original environment with
    ``result.update_environ()`` if you like, but it's probably
    a bad idea to modify ``os.environ`` in that way because
    the calling app won't want to have the project
    environment.

    The ``environ`` should usually be kept between
    preparations, starting out as ``os.environ`` but then
    being modified by the user.

    If the project has a non-empty ``problems`` attribute,
    this function returns the project problems inside a failed
    result. So ``project.problems`` does not need to be checked in
    advance.

    Args:
        project (Project): from the ``load_project`` method
        environ (dict): os.environ or the previously-prepared environ; not modified in-place
        mode (str): mode from ``PROVIDE_MODE_PRODUCTION``, ``PROVIDE_MODE_DEVELOPMENT``, ``PROVIDE_MODE_CHECK``
        provide_whitelist (iterable of str): ONLY call provide() for the listed env vars' requirements
        extra_command_args (list): extra args to include in the returned command argv

    Returns:
        a ``PrepareResult`` instance, which has a ``failed`` flag

    """
    failure = _project_problems_to_prepare_failure(project)
    if failure is not None:
        return failure

    stage = prepare_in_stages(project,
                              environ,
                              keep_going_until_success=False,
                              mode=mode,
                              provide_whitelist=provide_whitelist,
                              extra_command_args=extra_command_args)

    return prepare_execute_without_interaction(stage)


def prepare_with_browser_ui(project,
                            environ=None,
                            extra_command_args=None,
                            keep_going_until_success=True,
                            io_loop=None,
                            show_url=None):
    """Prepare a project to run one of its commands.

    This method can interact with the user via a browser-based UI.

    This method returns a result object. The result object has
    a ``failed`` property.  If the result is failed, the
    ``errors`` property has the errors.  If the result is not
    failed, the ``command_exec_info`` property has the stuff
    you need to run the project's default command, and the
    ``environ`` property has the updated environment. The
    passed-in ``environ`` is not modified in-place.

    You can update your original environment with
    ``result.update_environ()`` if you like, but it's probably
    a bad idea to modify ``os.environ`` in that way because
    the calling app won't want to have the project
    environment.

    The ``environ`` should usually be kept between
    preparations, starting out as ``os.environ`` but then
    being modified by the user.

    If the project has a non-empty ``problems`` attribute,
    this function returns the project problems inside a failed
    result. So ``project.problems`` does not need to be checked in
    advance.

    Args:
        project (Project): from the ``load_project`` method
        environ (dict): os.environ or the previously-prepared environ; not modified in-place
        extra_command_args (list): extra args to include in the returned command argv
        keep_going_until_success (bool): whether to loop until requirements are met
        io_loop (IOLoop): tornado IOLoop to use, None for default
        show_url (function): function that's passed the URL to open it for the user

    Returns:
        a ``PrepareResult`` instance, which has a ``failed`` flag

    """
    failure = _project_problems_to_prepare_failure(project)
    if failure is not None:
        return failure

    stage = prepare_in_stages(project,
                              environ,
                              keep_going_until_success=keep_going_until_success,
                              mode=PROVIDE_MODE_DEVELOPMENT,
                              extra_command_args=extra_command_args)

    return prepare_execute_with_browser_ui(project, stage, io_loop=io_loop, show_url=show_url)


def prepare_execute_without_interaction(stage):
    """Advance through the PrepareStage without any interactivity.

    Returns:
       a ``PrepareResult`` instance
    """
    result = None
    while stage is not None:
        next_stage = stage.execute()
        result = stage.result
        if result.failed:
            break
        stage = next_stage
    return result


def prepare_execute_with_browser_ui(project, stage, io_loop=None, show_url=None):
    """Advance through the PrepareStage using a browser UI.

    This may need to ask the user questions, may start services,
    run scripts, load configuration, install packages... it can do
    anything. Expect side effects.

    Args:
        project (Project): the project
        stage (PrepareStage): from prepare_in_stages()
        io_loop (IOLoop): tornado IOLoop to use, None for default
        show_url (function): takes a URL and displays it in a browser somehow, None for default

    Returns:
        a ``PrepareResult`` instance

    """
    return prepare_ui.prepare_browser(project=project, stage=stage, io_loop=io_loop, show_url=show_url)


def unprepare(project):
    """Attempt to clean up project-scoped resources allocated by prepare().

    This will retain any user configuration choices about how to
    provide requirements, but it stops project-scoped services.
    Global system services or other services potentially shared
    among projects will not be stopped.

    Args:
        project (Project): the project

    """
    local_state = LocalStateFile.load_for_directory(project.directory_path)

    run_states = local_state.get_all_service_run_states()
    for service_name in copy(run_states):
        state = run_states[service_name]
        if 'shutdown_commands' in state:
            commands = state['shutdown_commands']
            for command in commands:
                print("Running " + repr(command))
                code = subprocess.call(command)
                print("  exited with " + str(code))
        # clear out the run state once we try to shut it down
        local_state.set_service_run_state(service_name, dict())
        local_state.save()
