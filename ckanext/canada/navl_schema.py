from pylons.i18n import _
from paste.deploy.converters import asbool
from ckan.logic.schema import (default_create_package_schema,
    default_update_package_schema, default_show_package_schema)
from ckan.logic.converters import (free_tags_only, convert_from_tags,
    convert_to_tags, convert_from_extras, convert_to_extras)
from ckan.lib.navl.validators import (ignore, ignore_missing, not_empty, empty,
    if_empty_same_as)
from ckan.logic.validators import (isodate, tag_string_convert,
    name_validator, package_name_validator, boolean_validator,
    owner_org_validator, duplicate_extras_key)
from ckan.lib.navl.dictization_functions import Invalid, missing
from ckan.new_authz import is_sysadmin
from ckan.model.package import Package
from ckan import model

from formencode.validators import OneOf

from ckanext.canada.metadata_schema import schema_description
from ckanext.canada.helpers import may_publish_datasets
from shapely.geometry import asShape
import json
import uuid

def create_package_schema():
    """
    Add our custom fields for validation from the form
    """
    schema = default_create_package_schema()
    _schema_update(schema, 'create')
    return schema

def update_package_schema():
    """
    Add our custom fields for validation from the form
    """
    schema = default_update_package_schema()
    _schema_update(schema, 'update')
    return schema

def show_package_schema():
    """
    Add our custom fields for converting from the db
    """
    schema = default_show_package_schema()
    _schema_update(schema, 'show')
    return schema




def _schema_update(schema, purpose):
    """
    :param schema: schema dict to update
    :param purpose: 'create', 'update' or 'show'
    """
    assert purpose in ('create', 'update', 'show')

    # XXX: remove duplicate_extras_key validation
    # to work around update failures on packages with
    # multiple portal_release_date values mistakenly added
    if '__before' in schema:
        schema['__before'] = [v for v in schema['__before']
            if v != duplicate_extras_key]

    if purpose == 'create':
        schema['id'] = [protect_new_dataset_id, if_empty_generate_uuid,
            unicode, name_validator, package_id_doesnt_exist]
        schema['name'] = [if_empty_same_as('id'), unicode, name_validator,
            package_name_validator]
    if purpose in ('create', 'update'):
        schema['title'] = [not_empty, unicode]
        schema['notes'] = [not_empty, unicode]
        schema['owner_org'] = [not_empty, owner_org_validator_publisher, unicode]
        schema['license_id'] = [not_empty, unicode]
    else:
        schema['author_email'] = [fixed_value(
            schema_description.dataset_field_by_id['author_email'])]
        schema['department_number'] = [get_department_number]
        schema['license_title_fra'] = [get_license_field('title_fra')]
        schema['license_url_fra'] = [get_license_field('url_fra')]

    resources = schema['resources']

    for name, lang, field in schema_description.dataset_field_iter():
        if name in schema:
            continue # don't modify other existing fields

        v = _schema_field_validators(name, lang, field)
        if v is not None:
            schema[name] = v[0] if purpose != 'show' else v[1]

        if field['type'] == 'choice' and purpose in ('create', 'update'):
            schema[name].extend([
                convert_pilot_uuid(field),
                OneOf([c['key'] for c in field['choices']])])

    for name, lang, field in schema_description.resource_field_iter():
        if field['mandatory'] and purpose in ('create', 'update'):
            resources[name] = [not_empty, unicode]
        if field['type'] == 'choice' and purpose in ('create', 'update'):
            resources[name].extend([
                convert_pilot_uuid(field),
                OneOf([c['key'] for c in field['choices']])])


def _schema_field_validators(name, lang, field):
    """
    return a tuple with lists of validators for the field:
    one for create/update and one for show, or None to leave
    both lists unchanged
    """
    if name == 'portal_release_date':
        return ([treat_missing_as_empty, protect_portal_release_date,
                 isodate, convert_to_extras],
                [convert_from_extras, ignore_missing])

    edit = []
    view = []
    if field['type'] in ('calculated', 'fixed') or not field['mandatory']:
        edit.append(ignore_missing)
    elif field['mandatory']:
        # FIXME: assuming dataset field passed!
        edit.append(not_empty)

    if field['type'] == 'date':
        edit.append(isodate)
    elif field['type'] == 'keywords':
        edit.append(keywords_validate)
    elif field['type'] == 'tag_vocabulary':
        return (edit + [convert_pilot_uuid_list(field),
                convert_to_tags(field['vocabulary'])],
            view + [convert_from_tags(field['vocabulary'])])
    elif field['type'] == 'boolean':
        edit.extend([unicode, boolean_validator])
        view.extend([convert_from_extras, ignore_missing, boolean_validator])
    elif field['type'] == 'fixed':
        edit.append(ignore)
        view.append(fixed_value(field, lang))
    elif field['type'] == 'geojson':
        edit.append(geojson_validator)
    else:
        edit.append(unicode)

    return (edit + [convert_to_extras],
            view if view else [convert_from_extras, ignore_missing])


def treat_missing_as_empty(key, data, errors, context):
    value = data.get(key, '')
    if value is missing:
        data[key] = ''

def protect_portal_release_date(key, data, errors, context):
    """
    Ensure the portal_release_date is not changed by an unauthorized user.
    """
    if is_sysadmin(context['user']):
        return
    original = ''
    package = context.get('package')
    if package:
        original = package.extras.get('portal_release_date', '')
    value = data.get(key, '')
    if original == value:
        return

    user = context['user']
    user = model.User.get(user)
    if may_publish_datasets(user):
        return

    if value == '':
        # silently replace with the old value when none is sent
        data[key] = original
        return

    raise Invalid('Cannot change value of key from %s to %s. '
                  'This key is read-only' % (original, value))


def ignore_missing_only_sysadmin(key, data, errors, context):
    """
    Ignore missing field *only* if the user is a sysadmin.
    """
    if is_sysadmin(context['user']):
        return ignore_missing(key, data, errors, context)


def keywords_validate(key, data, errors, context):
    """
    Validate a keywords in the same way as tag_string, but don't
    insert tags into data.
    """
    # also allow apostrophe, can't avoid them with French keywords
    data = {key: data[key].replace("'", "-")}
    try:
        tag_string_convert(key, data, errors, context)
    except Invalid, e:
        e.error = e.error.replace("-_.", "' - _ .")
        raise e


def protect_new_dataset_id(key, data, errors, context):
    """
    Allow dataset ids to be set for packages created by a sysadmin
    """
    if not data[key] or data[key] is missing:
        return
    if is_sysadmin(context['user']):
        return
    empty(key, data, errors, context)


def if_empty_generate_uuid(value):
    """
    Generate a uuid for this dataset early so that it may be
    copied into the name field.
    """
    if not value or value is missing:
        return str(uuid.uuid4())
    return value


def package_id_doesnt_exist(key, data, errors, context):
    """
    fail if this value already exists as a package id.
    """
    # XXX: this is not a solution for the race where two packages with
    # the same id are created at the same time.  When that happens one
    # will silently overwrite the other :-(

    model = context["model"]
    session = context["session"]
    existing = model.Package.get(data[key])
    if existing:
        errors[key].append(_('That URL is already in use.'))


def convert_pilot_uuid(field):
    """
    Allow the user to pass a pilot UUID instead of one of the normal choices
    by replacing it with the correct key value
    """
    mapping = field['choices_by_pilot_uuid']
    def handle_pilot_uuid(value):
        if value in mapping:
            return mapping[value]['key']
        return value
    return handle_pilot_uuid


def convert_pilot_uuid_list(field):
    """
    Allow the user to pass a pilot UUID instead of one of the normal choices
    in a list by replacing the values with the correct key values
    """
    mapping = field['choices_by_pilot_uuid']
    def handle_pilot_uuid_list(value):
        if not value:
            return []
        if isinstance(value, (str, unicode)):
            value = [v.strip() for v in value.split(',')]
        return [mapping.get(v, {'key':v})['key'] for v in value]
    return handle_pilot_uuid_list


def fixed_value(field, lang=None):
    """
    Generate the same value for this field for all datasets
    """
    def use_example_value(value):
        return field['example']['fra' if lang == 'fra' else 'eng']
    return use_example_value


def get_department_number(key, data, errors, context):
    """
    Return the department number for the org to which this dataset is assigned
    """
    owner_org = data.get(('owner_org',))
    # FIXME: only returns department number for orgs from pilot
    data[key] = schema_description.dataset_field_by_id['owner_org'][
        'choices_by_pilot_uuid'].get(owner_org, {}).get('id')


def get_license_field(name):
    """
    Fill in a value from the selected license
    """
    def fill_license_value(key, data, errors, context):
        license_id = data.get(('license_id',))
        value = None
        if license_id:
            license = Package.get_license_register().get(license_id)
            if license:
                value = license[name]
        data[key] = value
    return fill_license_value

def owner_org_validator_publisher(key, data, errors, context):
    """
    owner_org_validator modified to allow publisher to update
    datasets for any org
    """

    value = data.get(key)

    if value is missing or not value:
        if not ckan.new_authz.check_config_permission('create_unowned_dataset'):
            raise Invalid(_('A organization must be supplied'))
        data.pop(key, None)
        raise StopOnError

    model = context['model']
    group = model.Group.get(value)
    if not group:
        raise Invalid(_('Organization does not exist'))
    group_id = group.id
    user = context['user']
    user = model.User.get(user)
    if not(may_publish_datasets(user) or user.is_in_group(group_id)):
        raise Invalid(_('You cannot add a dataset to this organization'))
    data[key] = group_id

def geojson_validator(value):
    if value:
        try:
            gjson = json.loads(value)
            shape = asShape(gjson)
        except ValueError:
            raise Invalid(_("Invalid GeoJSON"))
    return value
