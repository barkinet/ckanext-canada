# -*- coding: utf-8 -*-
import hashlib
import calendar
import datetime

import paste.script
from pylons import config
from ckan.lib.cli import CkanCommand

from ckanapi import LocalCKAN

DATASET_TYPES = ['ati-summaries', 'ati-none']
BATCH_SIZE = 1000
WINDOW_YEARS = 2
MONTHS_FRA = [
    u'', # "month 0"
    u'janvier',
    u'février',
    u'mars',
    u'avril',
    u'mai',
    u'juin',
    u'juillet',
    u'août',
    u'septembre',
    u'octobre',
    u'novembre',
    u'décembre',
    ]

class ATICommand(CkanCommand):
    """
    Manage the ATI Summaries SOLR index

    Usage::

        paster ati clear
                   rebuild
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__

    parser = paste.script.command.Command.standard_parser(verbose=True)
    parser.add_option('-c', '--config', dest='config',
        default='development.ini', help='Config file to use.')

    def command(self):
        if not self.args or self.args[0] in ['--help', '-h', 'help']:
            print self.__doc__
            return

        cmd = self.args[0]
        self._load_config()

        if cmd == 'clear':
            return self._clear_index()
        elif cmd == 'rebuild':
            return self._rebuild()

    def _clear_index(self):
        conn = _solr_connection()
        conn.delete_query("*:*")
        conn.commit()

    def _rebuild(self):
        conn = _solr_connection()
        lc = LocalCKAN()
        for org in lc.action.organization_list():
            count = 0
            org_detail = lc.action.organization_show(id=org)
            for records in _ati_summaries(org_detail, lc):
                _update_records(records, org_detail, conn)
                count += len(records)

            print org, count
        #rval = conn.query("*:*" , rows=2)

def _solr_connection():
    from solr import SolrConnection
    url = config['ati_summaries.solr_url']
    user = config.get('ati_summaries.solr_user')
    password = config.get('ati_summaries.solr_password')
    if user is not None and password is not None:
        return SolrConnection(url, http_user=user, http_pass=password)
    return SolrConnection(url)

def _ati_summaries(org, lc):
    """
    generator of ati summary dicts for organization with name org
    """
    for dataset_type in DATASET_TYPES:
        result = lc.action.package_search(
            q="type:%s owner_org:%s" % (dataset_type, org['id']),
            rows=1000)['results']
        resource_id = result[0]['resources'][0]['id']
        offset = 0
        while True:
            rval = lc.action.datastore_search(resource_id=resource_id,
                limit=BATCH_SIZE, offset=offset)
            records = rval['records']
            if not records:
                break
            yield records
            offset += len(records)

def _update_records(records, org_detail, conn):
    """
    """
    out = []
    org = org_detail['name']
    orghash = hashlib.md5(org).hexdigest()
    today = datetime.datetime.today()
    start_year_month = (today.year - WINDOW_YEARS, today.month)
    for r in records:
        if (r['year'], r['month']) < start_year_month:
            continue
        unique = hashlib.md5(orghash
            + r.get('request_number', repr((r['year'], r['month']))
            ).encode('utf-8')).hexdigest()
        shortform = None
        shortform_fr = None
        ati_email = None
        month = max(1, min(12, r['month']))
        for e in org_detail['extras']:
            if e['key'] == 'shortform':
                shortform = e['value']
            elif e['key'] == 'shortform_fr':
                shortform_fr = e['value']
            elif e['key'] == 'ati_email':
                ati_email = e['value']

        # don't ask why, just doing it the way it was done before
        out.append({
            'bundle': 'ati_summaries',
            'hash': 'avexlb',
            'id': unique + 'en',
            'i18n_ts_en_ati_request_number': r.get('request_number', ''),
            'i18n_ts_en_ati_request_summary': r.get('summary_eng', ''),
            'ss_ati_contact_information_en':
                "http://data.gc.ca/data/en/organization/about/{0}"
                .format(org),
            'ss_ati_disposition_en': r.get('disposition', '').split(' / ', 1)[0],
            'ss_ati_month_en': '{0:02d}'.format(r['month']),
            'ss_ati_monthname_en': calendar.month_name[month],
            'ss_ati_number_of_pages_en': r.get('pages', ''),
            'ss_ati_organization_en': org_detail['title'].split(' | ', 1)[0],
            'ss_ati_year_en': r['year'],
            'ss_ati_org_shortform_en': shortform,
            'ss_ati_contact_email_en': ati_email,
            'ss_ati_nothing_to_report_en': ('' if 'request_number' in r else
                'Nothing to report this month'),
            'ss_language': 'en',
            })
        out.append({
            'bundle': 'ati_summaries',
            'hash': 'avexlb',
            'id': unique + 'fr',
            'i18n_ts_fr_ati_request_number': r.get('request_number', ''),
            'i18n_ts_fr_ati_request_summary': r.get('summary_fra', ''),
            'ss_ati_contact_information_fr':
                "http://donnees.gc.ca/data/fr/organization/about/{0}"
                .format(org),
            'ss_ati_disposition_fr': r.get('disposition', '').split(' / ', 1)[-1],
            'ss_ati_month_fr': '{0:02d}'.format(r['month']),
            'ss_ati_monthname_fr': MONTHS_FRA[month],
            'ss_ati_number_of_pages_fr': r.get('pages', ''),
            'ss_ati_organization_fr': org_detail['title'].split(' | ', 1)[-1],
            'ss_ati_year_fr': r['year'],
            'ss_ati_org_shortform_fr': shortform_fr,
            'ss_ati_contact_email_fr': ati_email,
            'ss_ati_nothing_to_report_fr': ('' if 'request_number' in r else
                u'Rien à signaler ce mois-ci'),
            'ss_language': 'fr',
            })
    conn.add_many(out, _commit=True)
