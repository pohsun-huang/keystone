# Copyright 2012 OpenStack Foundation
# Copyright 2012 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import uuid

from six.moves import http_client

from keystone.catalog import schema
from keystone.common import controller
from keystone.common import dependency
from keystone.common import utils
from keystone.common import validation
from keystone.common import wsgi
from keystone import exception
from keystone.i18n import _
from keystone import notifications
from keystone import resource


INTERFACES = ['public', 'internal', 'admin']


@dependency.requires('catalog_api')
class Service(controller.V2Controller):

    @controller.v2_deprecated
    def get_services(self, request):
        self.assert_admin(request)
        service_list = self.catalog_api.list_services()
        return {'OS-KSADM:services': service_list}

    @controller.v2_deprecated
    def get_service(self, request, service_id):
        self.assert_admin(request)
        service_ref = self.catalog_api.get_service(service_id)
        return {'OS-KSADM:service': service_ref}

    @controller.v2_deprecated
    def delete_service(self, request, service_id):
        self.assert_admin(request)
        initiator = notifications._get_request_audit_info(request.context_dict)
        self.catalog_api.delete_service(service_id, initiator)

    @controller.v2_deprecated
    def create_service(self, request, OS_KSADM_service):
        validation.lazy_validate(schema.service_create_v2, OS_KSADM_service)
        self.assert_admin(request)
        service_id = uuid.uuid4().hex
        service_ref = OS_KSADM_service.copy()
        service_ref['id'] = service_id
        initiator = notifications._get_request_audit_info(request.context_dict)
        new_service_ref = self.catalog_api.create_service(
            service_id, service_ref, initiator)
        return {'OS-KSADM:service': new_service_ref}


@dependency.requires('catalog_api')
class Endpoint(controller.V2Controller):

    @controller.v2_deprecated
    def get_endpoints(self, request):
        """Merge matching v3 endpoint refs into legacy refs."""
        self.assert_admin(request)
        legacy_endpoints = {}
        v3_endpoints = {}
        for endpoint in self.catalog_api.list_endpoints():
            if not endpoint.get('legacy_endpoint_id'):  # pure v3 endpoint
                # tell endpoints apart by the combination of
                # service_id and region_id.
                # NOTE(muyu): in theory, it's possible that there are more than
                # one endpoint of one service, one region and one interface,
                # but in practice, it makes no sense because only one will be
                # used.
                key = (endpoint['service_id'], endpoint['region_id'])
                v3_endpoints.setdefault(key, []).append(endpoint)
            else:  # legacy endpoint
                if endpoint['legacy_endpoint_id'] not in legacy_endpoints:
                    legacy_ep = endpoint.copy()
                    legacy_ep['id'] = legacy_ep.pop('legacy_endpoint_id')
                    legacy_ep.pop('interface')
                    legacy_ep.pop('url')
                    legacy_ep['region'] = legacy_ep.pop('region_id')

                    legacy_endpoints[endpoint['legacy_endpoint_id']] = (
                        legacy_ep)
                else:
                    legacy_ep = (
                        legacy_endpoints[endpoint['legacy_endpoint_id']])

                # add the legacy endpoint with an interface url
                legacy_ep['%surl' % endpoint['interface']] = endpoint['url']

        # convert collected v3 endpoints into v2 endpoints
        for endpoints in v3_endpoints.values():
            legacy_ep = {}
            # For v3 endpoints in the same group, contents of extra attributes
            # can be different, which may cause confusion if a random one is
            # used. So only necessary attributes are used here.
            # It's different for legacy v2 endpoints, which are created
            # with the same "extra" value when being migrated.
            for key in ('service_id', 'enabled'):
                legacy_ep[key] = endpoints[0][key]
            legacy_ep['region'] = endpoints[0]['region_id']
            for endpoint in endpoints:
                # Public URL is required for v2 endpoints, so the generated v2
                # endpoint uses public endpoint's id as its id, which can also
                # be an indicator whether a public v3 endpoint is present.
                # It's safe to do so is also because that there is no v2 API to
                # get an endpoint by endpoint ID.
                if endpoint['interface'] == 'public':
                    legacy_ep['id'] = endpoint['id']
                legacy_ep['%surl' % endpoint['interface']] = endpoint['url']

            # this means there is no public URL of this group of v3 endpoints
            if 'id' not in legacy_ep:
                continue
            legacy_endpoints[legacy_ep['id']] = legacy_ep
        return {'endpoints': list(legacy_endpoints.values())}

    @controller.v2_deprecated
    def create_endpoint(self, request, endpoint):
        """Create three v3 endpoint refs based on a legacy ref."""
        self.assert_admin(request)

        # according to the v2 spec publicurl is mandatory
        self._require_attribute(endpoint, 'publicurl')
        # service_id is necessary
        self._require_attribute(endpoint, 'service_id')

        # we should check publicurl, adminurl, internalurl
        # if invalid, we should raise an exception to reject
        # the request
        for interface in INTERFACES:
            interface_url = endpoint.get(interface + 'url')
            if interface_url:
                utils.check_endpoint_url(interface_url)

        initiator = notifications._get_request_audit_info(request.context_dict)

        if endpoint.get('region') is not None:
            try:
                self.catalog_api.get_region(endpoint['region'])
            except exception.RegionNotFound:
                region = dict(id=endpoint['region'])
                self.catalog_api.create_region(region, initiator)

        legacy_endpoint_ref = endpoint.copy()

        urls = {}
        for i in INTERFACES:
            # remove all urls so they aren't persisted them more than once
            url = '%surl' % i
            if endpoint.get(url):
                # valid urls need to be persisted
                urls[i] = endpoint.pop(url)
            elif url in endpoint:
                # null or empty urls can be discarded
                endpoint.pop(url)
                legacy_endpoint_ref.pop(url)

        legacy_endpoint_id = uuid.uuid4().hex
        for interface, url in urls.items():
            endpoint_ref = endpoint.copy()
            endpoint_ref['id'] = uuid.uuid4().hex
            endpoint_ref['legacy_endpoint_id'] = legacy_endpoint_id
            endpoint_ref['interface'] = interface
            endpoint_ref['url'] = url
            endpoint_ref['region_id'] = endpoint_ref.pop('region')
            self.catalog_api.create_endpoint(endpoint_ref['id'], endpoint_ref,
                                             initiator)

        legacy_endpoint_ref['id'] = legacy_endpoint_id
        return {'endpoint': legacy_endpoint_ref}

    @controller.v2_deprecated
    def delete_endpoint(self, request, endpoint_id):
        """Delete up to three v3 endpoint refs based on a legacy ref ID."""
        self.assert_admin(request)
        initiator = notifications._get_request_audit_info(request.context_dict)

        deleted_at_least_one = False
        for endpoint in self.catalog_api.list_endpoints():
            if endpoint['legacy_endpoint_id'] == endpoint_id:
                self.catalog_api.delete_endpoint(endpoint['id'], initiator)
                deleted_at_least_one = True

        if not deleted_at_least_one:
            raise exception.EndpointNotFound(endpoint_id=endpoint_id)


@dependency.requires('catalog_api')
class RegionV3(controller.V3Controller):
    collection_name = 'regions'
    member_name = 'region'

    def create_region_with_id(self, context, region_id, region):
        """Create a region with a user-specified ID.

        This method is unprotected because it depends on ``self.create_region``
        to enforce policy.
        """
        if 'id' in region and region_id != region['id']:
            raise exception.ValidationError(
                _('Conflicting region IDs specified: '
                  '"%(url_id)s" != "%(ref_id)s"') % {
                      'url_id': region_id,
                      'ref_id': region['id']})
        region['id'] = region_id
        return self.create_region(context, region)

    @controller.protected()
    def create_region(self, request, region):
        validation.lazy_validate(schema.region_create, region)
        ref = self._normalize_dict(region)

        if not ref.get('id'):
            ref = self._assign_unique_id(ref)

        initiator = notifications._get_request_audit_info(request.context_dict)
        ref = self.catalog_api.create_region(ref, initiator)
        return wsgi.render_response(
            RegionV3.wrap_member(request.context_dict, ref),
            status=(http_client.CREATED,
                    http_client.responses[http_client.CREATED]))

    @controller.filterprotected('parent_region_id')
    def list_regions(self, request, filters):
        hints = RegionV3.build_driver_hints(request, filters)
        refs = self.catalog_api.list_regions(hints)
        return RegionV3.wrap_collection(request.context_dict,
                                        refs,
                                        hints=hints)

    @controller.protected()
    def get_region(self, request, region_id):
        ref = self.catalog_api.get_region(region_id)
        return RegionV3.wrap_member(request.context_dict, ref)

    @controller.protected()
    def update_region(self, request, region_id, region):
        validation.lazy_validate(schema.region_update, region)
        self._require_matching_id(region_id, region)
        initiator = notifications._get_request_audit_info(request.context_dict)
        ref = self.catalog_api.update_region(region_id, region, initiator)
        return RegionV3.wrap_member(request.context_dict, ref)

    @controller.protected()
    def delete_region(self, request, region_id):
        initiator = notifications._get_request_audit_info(request.context_dict)
        return self.catalog_api.delete_region(region_id, initiator)


@dependency.requires('catalog_api')
class ServiceV3(controller.V3Controller):
    collection_name = 'services'
    member_name = 'service'

    def __init__(self):
        super(ServiceV3, self).__init__()
        self.get_member_from_driver = self.catalog_api.get_service

    @controller.protected()
    def create_service(self, request, service):
        validation.lazy_validate(schema.service_create, service)
        ref = self._assign_unique_id(self._normalize_dict(service))
        initiator = notifications._get_request_audit_info(request.context_dict)
        ref = self.catalog_api.create_service(ref['id'], ref, initiator)
        return ServiceV3.wrap_member(request.context_dict, ref)

    @controller.filterprotected('type', 'name')
    def list_services(self, request, filters):
        hints = ServiceV3.build_driver_hints(request, filters)
        refs = self.catalog_api.list_services(hints=hints)
        return ServiceV3.wrap_collection(request.context_dict,
                                         refs,
                                         hints=hints)

    @controller.protected()
    def get_service(self, request, service_id):
        ref = self.catalog_api.get_service(service_id)
        return ServiceV3.wrap_member(request.context_dict, ref)

    @controller.protected()
    def update_service(self, request, service_id, service):
        validation.lazy_validate(schema.service_update, service)
        self._require_matching_id(service_id, service)
        initiator = notifications._get_request_audit_info(request.context_dict)
        ref = self.catalog_api.update_service(service_id, service, initiator)
        return ServiceV3.wrap_member(request.context_dict, ref)

    @controller.protected()
    def delete_service(self, request, service_id):
        initiator = notifications._get_request_audit_info(request.context_dict)
        return self.catalog_api.delete_service(service_id, initiator)


@dependency.requires('catalog_api')
class EndpointV3(controller.V3Controller):
    collection_name = 'endpoints'
    member_name = 'endpoint'

    def __init__(self):
        super(EndpointV3, self).__init__()
        self.get_member_from_driver = self.catalog_api.get_endpoint

    @classmethod
    def filter_endpoint(cls, ref):
        if 'legacy_endpoint_id' in ref:
            ref.pop('legacy_endpoint_id')
        ref['region'] = ref['region_id']
        return ref

    @classmethod
    def wrap_member(cls, context, ref):
        ref = cls.filter_endpoint(ref)
        return super(EndpointV3, cls).wrap_member(context, ref)

    def _validate_endpoint_region(self, endpoint, context=None):
        """Ensure the region for the endpoint exists.

        If 'region_id' is used to specify the region, then we will let the
        manager/driver take care of this.  If, however, 'region' is used,
        then for backward compatibility, we will auto-create the region.

        """
        if (endpoint.get('region_id') is None and
                endpoint.get('region') is not None):
            # To maintain backward compatibility with clients that are
            # using the v3 API in the same way as they used the v2 API,
            # create the endpoint region, if that region does not exist
            # in keystone.
            endpoint['region_id'] = endpoint.pop('region')
            try:
                self.catalog_api.get_region(endpoint['region_id'])
            except exception.RegionNotFound:
                region = dict(id=endpoint['region_id'])
                initiator = notifications._get_request_audit_info(context)
                self.catalog_api.create_region(region, initiator)

        return endpoint

    @controller.protected()
    def create_endpoint(self, request, endpoint):
        validation.lazy_validate(schema.endpoint_create, endpoint)
        utils.check_endpoint_url(endpoint['url'])
        ref = self._assign_unique_id(self._normalize_dict(endpoint))
        ref = self._validate_endpoint_region(ref, request.context_dict)
        initiator = notifications._get_request_audit_info(request.context_dict)
        ref = self.catalog_api.create_endpoint(ref['id'], ref, initiator)
        return EndpointV3.wrap_member(request.context_dict, ref)

    @controller.filterprotected('interface', 'service_id', 'region_id')
    def list_endpoints(self, request, filters):
        hints = EndpointV3.build_driver_hints(request, filters)
        refs = self.catalog_api.list_endpoints(hints=hints)
        return EndpointV3.wrap_collection(request.context_dict,
                                          refs,
                                          hints=hints)

    @controller.protected()
    def get_endpoint(self, request, endpoint_id):
        ref = self.catalog_api.get_endpoint(endpoint_id)
        return EndpointV3.wrap_member(request.context_dict, ref)

    @controller.protected()
    def update_endpoint(self, request, endpoint_id, endpoint):
        validation.lazy_validate(schema.endpoint_update, endpoint)
        self._require_matching_id(endpoint_id, endpoint)

        endpoint = self._validate_endpoint_region(endpoint.copy(),
                                                  request.context_dict)

        initiator = notifications._get_request_audit_info(request.context_dict)
        ref = self.catalog_api.update_endpoint(endpoint_id, endpoint,
                                               initiator)
        return EndpointV3.wrap_member(request.context_dict, ref)

    @controller.protected()
    def delete_endpoint(self, request, endpoint_id):
        initiator = notifications._get_request_audit_info(request.context_dict)
        return self.catalog_api.delete_endpoint(endpoint_id, initiator)


@dependency.requires('catalog_api', 'resource_api')
class EndpointFilterV3Controller(controller.V3Controller):

    def __init__(self):
        super(EndpointFilterV3Controller, self).__init__()
        notifications.register_event_callback(
            notifications.ACTIONS.deleted, 'project',
            self._on_project_or_endpoint_delete)
        notifications.register_event_callback(
            notifications.ACTIONS.deleted, 'endpoint',
            self._on_project_or_endpoint_delete)

    def _on_project_or_endpoint_delete(self, service, resource_type, operation,
                                       payload):
        project_or_endpoint_id = payload['resource_info']
        if resource_type == 'project':
            self.catalog_api.delete_association_by_project(
                project_or_endpoint_id)
        else:
            self.catalog_api.delete_association_by_endpoint(
                project_or_endpoint_id)

    @controller.protected()
    def add_endpoint_to_project(self, request, project_id, endpoint_id):
        """Establish an association between an endpoint and a project."""
        # NOTE(gyee): we just need to make sure endpoint and project exist
        # first. We don't really care whether if project is disabled.
        # The relationship can still be established even with a disabled
        # project as there are no security implications.
        self.catalog_api.get_endpoint(endpoint_id)
        self.resource_api.get_project(project_id)
        self.catalog_api.add_endpoint_to_project(endpoint_id,
                                                 project_id)

    @controller.protected()
    def check_endpoint_in_project(self, request, project_id, endpoint_id):
        """Verify endpoint is currently associated with given project."""
        self.catalog_api.get_endpoint(endpoint_id)
        self.resource_api.get_project(project_id)
        self.catalog_api.check_endpoint_in_project(endpoint_id,
                                                   project_id)

    @controller.protected()
    def list_endpoints_for_project(self, request, project_id):
        """List all endpoints currently associated with a given project."""
        self.resource_api.get_project(project_id)
        filtered_endpoints = self.catalog_api.list_endpoints_for_project(
            project_id)

        return EndpointV3.wrap_collection(
            request.context_dict,
            [v for v in filtered_endpoints.values()])

    @controller.protected()
    def remove_endpoint_from_project(self, request, project_id, endpoint_id):
        """Remove the endpoint from the association with given project."""
        self.catalog_api.remove_endpoint_from_project(endpoint_id,
                                                      project_id)

    @controller.protected()
    def list_projects_for_endpoint(self, request, endpoint_id):
        """Return a list of projects associated with the endpoint."""
        self.catalog_api.get_endpoint(endpoint_id)
        refs = self.catalog_api.list_projects_for_endpoint(endpoint_id)

        projects = [self.resource_api.get_project(
            ref['project_id']) for ref in refs]
        return resource.controllers.ProjectV3.wrap_collection(
            request.context_dict, projects)


@dependency.requires('catalog_api', 'resource_api')
class EndpointGroupV3Controller(controller.V3Controller):
    collection_name = 'endpoint_groups'
    member_name = 'endpoint_group'

    VALID_FILTER_KEYS = ['service_id', 'region_id', 'interface']

    def __init__(self):
        super(EndpointGroupV3Controller, self).__init__()

    @classmethod
    def base_url(cls, context, path=None):
        """Construct a path and pass it to V3Controller.base_url method."""
        path = '/OS-EP-FILTER/' + cls.collection_name
        return super(EndpointGroupV3Controller, cls).base_url(context,
                                                              path=path)

    @controller.protected()
    def create_endpoint_group(self, request, endpoint_group):
        """Create an Endpoint Group with the associated filters."""
        validation.lazy_validate(schema.endpoint_group_create, endpoint_group)
        ref = self._assign_unique_id(self._normalize_dict(endpoint_group))
        self._require_attribute(ref, 'filters')
        self._require_valid_filter(ref)
        ref = self.catalog_api.create_endpoint_group(ref['id'], ref)
        return EndpointGroupV3Controller.wrap_member(request.context_dict, ref)

    def _require_valid_filter(self, endpoint_group):
        filters = endpoint_group.get('filters')
        for key in filters.keys():
            if key not in self.VALID_FILTER_KEYS:
                raise exception.ValidationError(
                    attribute=self._valid_filter_keys(),
                    target='endpoint_group')

    def _valid_filter_keys(self):
        return ' or '.join(self.VALID_FILTER_KEYS)

    @controller.protected()
    def get_endpoint_group(self, request, endpoint_group_id):
        """Retrieve the endpoint group associated with the id if exists."""
        ref = self.catalog_api.get_endpoint_group(endpoint_group_id)
        return EndpointGroupV3Controller.wrap_member(
            request.context_dict, ref)

    @controller.protected()
    def update_endpoint_group(self, request, endpoint_group_id,
                              endpoint_group):
        """Update fixed values and/or extend the filters."""
        validation.lazy_validate(schema.endpoint_group_update, endpoint_group)
        if 'filters' in endpoint_group:
            self._require_valid_filter(endpoint_group)
        ref = self.catalog_api.update_endpoint_group(endpoint_group_id,
                                                     endpoint_group)
        return EndpointGroupV3Controller.wrap_member(
            request.context_dict, ref)

    @controller.protected()
    def delete_endpoint_group(self, request, endpoint_group_id):
        """Delete endpoint_group."""
        self.catalog_api.delete_endpoint_group(endpoint_group_id)

    @controller.protected()
    def list_endpoint_groups(self, request):
        """List all endpoint groups."""
        refs = self.catalog_api.list_endpoint_groups()
        return EndpointGroupV3Controller.wrap_collection(
            request.context_dict, refs)

    @controller.protected()
    def list_endpoint_groups_for_project(self, request, project_id):
        """List all endpoint groups associated with a given project."""
        return EndpointGroupV3Controller.wrap_collection(
            request.context_dict,
            self.catalog_api.get_endpoint_groups_for_project(project_id))

    @controller.protected()
    def list_projects_associated_with_endpoint_group(self,
                                                     request,
                                                     endpoint_group_id):
        """List all projects associated with endpoint group."""
        endpoint_group_refs = (self.catalog_api.
                               list_projects_associated_with_endpoint_group(
                                   endpoint_group_id))
        projects = []
        for endpoint_group_ref in endpoint_group_refs:
            project = self.resource_api.get_project(
                endpoint_group_ref['project_id'])
            if project:
                projects.append(project)
        return resource.controllers.ProjectV3.wrap_collection(
            request.context_dict, projects)

    @controller.protected()
    def list_endpoints_associated_with_endpoint_group(self,
                                                      request,
                                                      endpoint_group_id):
        """List all the endpoints filtered by a specific endpoint group."""
        filtered_endpoints = (self.catalog_api.
                              get_endpoints_filtered_by_endpoint_group(
                                  endpoint_group_id))
        return EndpointV3.wrap_collection(request.context_dict,
                                          filtered_endpoints)


@dependency.requires('catalog_api', 'resource_api')
class ProjectEndpointGroupV3Controller(controller.V3Controller):
    collection_name = 'project_endpoint_groups'
    member_name = 'project_endpoint_group'

    def __init__(self):
        super(ProjectEndpointGroupV3Controller, self).__init__()
        notifications.register_event_callback(
            notifications.ACTIONS.deleted, 'project',
            self._on_project_delete)

    def _on_project_delete(self, service, resource_type,
                           operation, payload):
        project_id = payload['resource_info']
        self.catalog_api.delete_endpoint_group_association_by_project(
            project_id)

    @controller.protected()
    def get_endpoint_group_in_project(self, request, endpoint_group_id,
                                      project_id):
        """Retrieve the endpoint group associated with the id if exists."""
        self.resource_api.get_project(project_id)
        self.catalog_api.get_endpoint_group(endpoint_group_id)
        ref = self.catalog_api.get_endpoint_group_in_project(
            endpoint_group_id, project_id)
        return ProjectEndpointGroupV3Controller.wrap_member(
            request.context_dict, ref)

    @controller.protected()
    def add_endpoint_group_to_project(self, request, endpoint_group_id,
                                      project_id):
        """Create an association between an endpoint group and project."""
        self.resource_api.get_project(project_id)
        self.catalog_api.get_endpoint_group(endpoint_group_id)
        self.catalog_api.add_endpoint_group_to_project(
            endpoint_group_id, project_id)

    @controller.protected()
    def remove_endpoint_group_from_project(self, request, endpoint_group_id,
                                           project_id):
        """Remove the endpoint group from associated project."""
        self.resource_api.get_project(project_id)
        self.catalog_api.get_endpoint_group(endpoint_group_id)
        self.catalog_api.remove_endpoint_group_from_project(
            endpoint_group_id, project_id)

    @classmethod
    def _add_self_referential_link(cls, context, ref):
        url = ('/OS-EP-FILTER/endpoint_groups/%(endpoint_group_id)s'
               '/projects/%(project_id)s' % {
                   'endpoint_group_id': ref['endpoint_group_id'],
                   'project_id': ref['project_id']})
        ref.setdefault('links', {})
        ref['links']['self'] = url
