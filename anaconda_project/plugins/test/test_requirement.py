# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
from anaconda_project.plugins.registry import PluginRegistry
from anaconda_project.plugins.requirement import EnvVarRequirement, UserConfigOverrides

from anaconda_project.internal.test.tmpfile_utils import tmp_local_state_file


def test_user_config_overrides():
    overrides = UserConfigOverrides()
    assert overrides.env_spec_name is None
    overrides = UserConfigOverrides(env_spec_name='foo')
    assert overrides.env_spec_name == 'foo'


def test_find_by_env_var_unknown():
    registry = PluginRegistry()
    found = registry.find_requirement_by_env_var(env_var='FOO', options=None)
    assert found is not None
    assert isinstance(found, EnvVarRequirement)
    assert found.env_var == 'FOO'
    assert "EnvVarRequirement(env_var='FOO')" == repr(found)


def test_find_by_service_type_unknown():
    registry = PluginRegistry()
    found = registry.find_requirement_by_service_type(service_type='blah', env_var='FOO', options=dict())
    assert found is None


def test_autoguess_encrypted_option():
    def req(env_var, options=None):
        return EnvVarRequirement(registry=PluginRegistry(), env_var=env_var, options=options)

    assert not req(env_var='FOO').encrypted
    assert req(env_var='FOO', options=dict(encrypted=True)).encrypted

    assert req(env_var='FOO_PASSWORD').encrypted
    assert req(env_var='FOO_SECRET').encrypted
    assert req(env_var='FOO_SECRET_KEY').encrypted
    assert req(env_var='FOO_ENCRYPTED').encrypted

    assert not req(env_var='FOO_PASSWORD', options=dict(encrypted=False)).encrypted
    assert not req(env_var='FOO_SECRET', options=dict(encrypted=False)).encrypted
    assert not req(env_var='FOO_SECRET_KEY', options=dict(encrypted=False)).encrypted
    assert not req(env_var='FOO_ENCRYPTED', options=dict(encrypted=False)).encrypted


def test_empty_variable_treated_as_unset():
    requirement = EnvVarRequirement(registry=PluginRegistry(), env_var='FOO')
    status = requirement.check_status(dict(FOO=''), tmp_local_state_file(), 'default', UserConfigOverrides())
    assert not status
    assert "Environment variable FOO is not set." == status.status_description
    assert [] == status.logs
    assert [] == status.errors
