# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2018-2019 OzzieIsaacs, cervinko, jkrehm, bodybybuddha, ok11,
#                            andy29485, idalin, Kyosfonica, wuqi, Kennyl, lemmsh,
#                            falgh1, grunjol, csitko, ytils, xybydy, trasba, vrabe,
#                            ruben-herold, marblepebble, JackED42, SiphonSquirrel,
#                            apetresc, nanu-c, mutschler, GammaC0de, vuolter
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

from __future__ import division, print_function, unicode_literals
import os
import re
import base64
import json
import time
import operator
from datetime import datetime, timedelta

from babel import Locale as LC
from babel.dates import format_datetime
from flask import Blueprint, flash, redirect, url_for, abort, request, make_response, send_from_directory, g, Response
from flask_login import login_required, current_user, logout_user, confirm_login
from flask_babel import gettext as _
from sqlalchemy import and_
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, OperationalError, InvalidRequestError
from sqlalchemy.sql.expression import func, or_, text

from . import constants, logger, helper, services
# from .cli import filepicker
from . import db, calibre_db, ub, web_server, get_locale, config, updater_thread, babel, gdriveutils
from .helper import check_valid_domain, send_test_mail, reset_password, generate_password_hash, check_email, \
    valid_email, check_username
from .gdriveutils import is_gdrive_ready, gdrive_support
from .render_template import render_title_template, get_sidebar_config
from . import debug_info, _BABEL_TRANSLATIONS

try:
    from functools import wraps
except ImportError:
    pass  # We're not using Python 3

log = logger.create()

feature_support = {
        'ldap': bool(services.ldap),
        'goodreads': bool(services.goodreads_support),
        'kobo':  bool(services.kobo),
        'updater': constants.UPDATER_AVAILABLE,
        'gmail': bool(services.gmail)
    }

try:
    import rarfile  # pylint: disable=unused-import
    feature_support['rar'] = True
except (ImportError, SyntaxError):
    feature_support['rar'] = False

try:
    from .oauth_bb import oauth_check, oauthblueprints
    feature_support['oauth'] = True
except ImportError as err:
    log.debug('Cannot import Flask-Dance, login with Oauth will not work: %s', err)
    feature_support['oauth'] = False
    oauthblueprints = []
    oauth_check = {}


feature_support['gdrive'] = gdrive_support
admi = Blueprint('admin', __name__)


def admin_required(f):
    """
    Checks if current_user.role == 1
    """

    @wraps(f)
    def inner(*args, **kwargs):
        if current_user.role_admin():
            return f(*args, **kwargs)
        abort(403)

    return inner


@admi.before_app_request
def before_request():
    if current_user.is_authenticated:
        confirm_login()
    g.constants = constants
    g.user = current_user
    g.allow_registration = config.config_public_reg
    g.allow_anonymous = config.config_anonbrowse
    g.allow_upload = config.config_uploading
    g.current_theme = config.config_theme
    g.config_authors_max = config.config_authors_max
    g.shelves_access = ub.session.query(ub.Shelf).filter(
        or_(ub.Shelf.is_public == 1, ub.Shelf.user_id == current_user.id)).order_by(ub.Shelf.name).all()
    if '/static/' not in request.path and not config.db_configured and \
        request.endpoint not in ('admin.ajax_db_config',
                                 'admin.simulatedbchange',
                                 'admin.db_configuration',
                                 'web.login',
                                 'web.logout',
                                 'admin.load_dialogtexts',
                                 'admin.ajax_pathchooser'):
        return redirect(url_for('admin.db_configuration'))


@admi.route("/admin")
@login_required
def admin_forbidden():
    abort(403)


@admi.route("/shutdown")
@login_required
@admin_required
def shutdown():
    task = int(request.args.get("parameter").strip())
    showtext = {}
    if task in {0, 1}:  # valid commandos received
        # close all database connections
        calibre_db.dispose()
        ub.dispose()

        if task == 0:
            showtext['text'] = _(u'Server restarted, please reload page')
        else:
            showtext['text'] = _(u'Performing shutdown of server, please close window')
        # stop gevent/tornado server
        web_server.stop(task==0)
        return json.dumps(showtext)

    if task == 2:
        log.warning("reconnecting to calibre database")
        calibre_db.reconnect_db(config, ub.app_DB_path)
        showtext['text'] = _(u'Reconnect successful')
        return json.dumps(showtext)

    showtext['text'] = _(u'Unknown command')
    return json.dumps(showtext), 400


@admi.route("/admin/view")
@login_required
@admin_required
def admin():
    version = updater_thread.get_current_version_info()
    if version is False:
        commit = _(u'Unknown')
    elif 'datetime' in version:
        commit = version['datetime']

        tz = timedelta(seconds=time.timezone if (time.localtime().tm_isdst == 0) else time.altzone)
        form_date = datetime.strptime(commit[:19], "%Y-%m-%dT%H:%M:%S")
        if len(commit) > 19:    # check if string has timezone
            if commit[19] == '+':
                form_date -= timedelta(hours=int(commit[20:22]), minutes=int(commit[23:]))
            elif commit[19] == '-':
                form_date += timedelta(hours=int(commit[20:22]), minutes=int(commit[23:]))
        commit = format_datetime(form_date - tz, format='short', locale=get_locale())
    else:
        commit = version['version']

    allUser = ub.session.query(ub.User).all()
    email_settings = config.get_mail_settings()
    kobo_support = feature_support['kobo'] and config.config_kobo_sync
    return render_title_template("admin.html", allUser=allUser, email=email_settings, config=config, commit=commit,
                                 feature_support=feature_support, kobo_support=kobo_support,
                                 title=_(u"Admin page"), page="admin")

@admi.route("/admin/dbconfig", methods=["GET", "POST"])
@login_required
@admin_required
def db_configuration():
    if request.method == "POST":
        return _db_configuration_update_helper()
    return _db_configuration_result()


@admi.route("/admin/config", methods=["GET"])
@login_required
@admin_required
def configuration():
    return render_title_template("config_edit.html",
                                 config=config,
                                 provider=oauthblueprints,
                                 feature_support=feature_support,
                                 title=_(u"Basic Configuration"), page="config")


@admi.route("/admin/ajaxconfig", methods=["POST"])
@login_required
@admin_required
def ajax_config():
    return _configuration_update_helper()


@admi.route("/admin/ajaxdbconfig", methods=["POST"])
@login_required
@admin_required
def ajax_db_config():
    return _db_configuration_update_helper()


@admi.route("/admin/alive", methods=["GET"])
@login_required
@admin_required
def calibreweb_alive():
    return "", 200

@admi.route("/admin/viewconfig")
@login_required
@admin_required
def view_configuration():
    read_column = calibre_db.session.query(db.Custom_Columns)\
        .filter(and_(db.Custom_Columns.datatype == 'bool', db.Custom_Columns.mark_for_delete == 0)).all()
    restrict_columns = calibre_db.session.query(db.Custom_Columns)\
        .filter(and_(db.Custom_Columns.datatype == 'text', db.Custom_Columns.mark_for_delete == 0)).all()
    return render_title_template("config_view_edit.html", conf=config, readColumns=read_column,
                                 restrictColumns=restrict_columns,
                                 title=_(u"UI Configuration"), page="uiconfig")

@admi.route("/admin/usertable")
@login_required
@admin_required
def edit_user_table():
    visibility = current_user.view_settings.get('useredit', {})
    languages = calibre_db.speaking_language()
    translations = babel.list_translations() + [LC('en')]
    allUser = ub.session.query(ub.User)
    tags = calibre_db.session.query(db.Tags)\
        .join(db.books_tags_link)\
        .join(db.Books)\
        .filter(calibre_db.common_filters()) \
        .group_by(text('books_tags_link.tag'))\
        .order_by(db.Tags.name).all()
    if config.config_restricted_column:
        custom_values = calibre_db.session.query(db.cc_classes[config.config_restricted_column]).all()
    else:
        custom_values = []
    if not config.config_anonbrowse:
        allUser = allUser.filter(ub.User.role.op('&')(constants.ROLE_ANONYMOUS) != constants.ROLE_ANONYMOUS)
    kobo_support = feature_support['kobo'] and config.config_kobo_sync
    return render_title_template("user_table.html",
                                 users=allUser.all(),
                                 tags=tags,
                                 custom_values=custom_values,
                                 translations=translations,
                                 languages=languages,
                                 visiblility=visibility,
                                 all_roles=constants.ALL_ROLES,
                                 kobo_support=kobo_support,
                                 sidebar_settings=constants.sidebar_settings,
                                 title=_(u"Edit Users"),
                                 page="usertable")


@admi.route("/ajax/listusers")
@login_required
@admin_required
def list_users():
    off = int(request.args.get("offset") or 0)
    limit = int(request.args.get("limit") or 10)
    search = request.args.get("search")
    sort = request.args.get("sort", "id")
    order = request.args.get("order", "").lower()
    state = None
    if sort == "state":
        state = json.loads(request.args.get("state", "[]"))

    if sort != "state" and order:
        order = text(f"{sort} {order}")
    elif not state:
        order = ub.User.id.asc()

    all_user = ub.session.query(ub.User)
    if not config.config_anonbrowse:
        all_user = all_user.filter(ub.User.role.op('&')(constants.ROLE_ANONYMOUS) != constants.ROLE_ANONYMOUS)

    total_count = filtered_count = all_user.count()

    if search:
        all_user = all_user.filter(
            or_(
                func.lower(ub.User.name).ilike(f"%{search}%"),
                func.lower(ub.User.kindle_mail).ilike(f"%{search}%"),
                func.lower(ub.User.email).ilike(f"%{search}%"),
            )
        )

    if state:
        users = calibre_db.get_checkbox_sorted(all_user.all(), state, off, limit, request.args.get("order", "").lower())
    else:
        users = all_user.order_by(order).offset(off).limit(limit).all()
    if search:
        filtered_count = len(users)

    for user in users:
        if user.default_language == "all":
            user.default = _("All")
        else:
            user.default = LC.parse(user.default_language).get_language_name(get_locale())

    table_entries = {'totalNotFiltered': total_count, 'total': filtered_count, "rows": users}
    js_list = json.dumps(table_entries, cls=db.AlchemyEncoder)
    response = make_response(js_list)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response

@admi.route("/ajax/deleteuser", methods=['POST'])
@login_required
@admin_required
def delete_user():
    user_ids = request.form.to_dict(flat=False)
    users = None
    if "userid[]" in user_ids:
        users = ub.session.query(ub.User).filter(ub.User.id.in_(user_ids['userid[]'])).all()
    elif "userid" in user_ids:
        users = ub.session.query(ub.User).filter(ub.User.id == user_ids['userid'][0]).all()
    count = 0
    errors = []
    success = []
    if not users:
        log.error("User not found")
        return Response(json.dumps({'type': "danger", 'message': _("User not found")}), mimetype='application/json')
    for user in users:
        try:
            message = _delete_user(user)
            count += 1
        except Exception as ex:
            log.error(ex)
            errors.append({'type': "danger", 'message': str(ex)})

    if count == 1:
        log.info(f"User {user_ids} deleted")
        success = [{'type': "success", 'message': message}]
    elif count > 1:
        log.info(f"Users {user_ids} deleted")
        success = [{'type': "success", 'message': _("{} users deleted successfully").format(count)}]
    success.extend(errors)
    return Response(json.dumps(success), mimetype='application/json')

@admi.route("/ajax/getlocale")
@login_required
@admin_required
def table_get_locale():
    locale = babel.list_translations() + [LC('en')]
    current_locale = get_locale()
    ret = [
        {'value': str(loc), 'text': loc.get_language_name(current_locale)}
        for loc in locale
    ]

    return json.dumps(ret)


@admi.route("/ajax/getdefaultlanguage")
@login_required
@admin_required
def table_get_default_lang():
    languages = calibre_db.speaking_language()
    ret = [{'value': 'all', 'text': _('Show All')}]
    ret.extend({'value': lang.lang_code, 'text': lang.name} for lang in languages)
    return json.dumps(ret)


@admi.route("/ajax/editlistusers/<param>", methods=['POST'])
@login_required
@admin_required
def edit_list_user(param):
    vals = request.form.to_dict(flat=False)
    all_user = ub.session.query(ub.User)
    if not config.config_anonbrowse:
        all_user = all_user.filter(ub.User.role.op('&')(constants.ROLE_ANONYMOUS) != constants.ROLE_ANONYMOUS)
    # only one user is posted
    if "pk" in vals:
        users = [all_user.filter(ub.User.id == vals['pk'][0]).one_or_none()]
    elif "pk[]" in vals:
        users = all_user.filter(ub.User.id.in_(vals['pk[]'])).all()
    else:
        return _("Malformed request"), 400
    if 'field_index' in vals:
        vals['field_index'] = vals['field_index'][0]
    if 'value' in vals:
        vals['value'] = vals['value'][0]
    elif 'value[]' not in vals:
        return _("Malformed request"), 400
    for user in users:
        try:
            if param in ['denied_tags', 'allowed_tags', 'allowed_column_value', 'denied_column_value']:
                if 'value[]' in vals:
                    setattr(user, param, prepare_tags(user, vals['action'][0], param, vals['value[]']))
                else:
                    setattr(user, param, vals['value'].strip())
            else:
                vals['value'] = vals['value'].strip()
                if param == 'name':
                    if user.name == "Guest":
                        raise Exception(_("Guest Name can't be changed"))
                    user.name = check_username(vals['value'])
                elif param =='email':
                    user.email = check_email(vals['value'])
                elif param =='kobo_only_shelves_sync':
                    user.kobo_only_shelves_sync = int(vals['value'] == 'true')
                elif param == 'kindle_mail':
                    user.kindle_mail = valid_email(vals['value']) if vals['value'] else ""
                elif param.endswith('role'):
                    value = int(vals['field_index'])
                    if user.name == "Guest" and value in \
                                 [constants.ROLE_ADMIN, constants.ROLE_PASSWD, constants.ROLE_EDIT_SHELFS]:
                        raise Exception(_("Guest can't have this role"))
                    if (
                        value <= 0
                        or value > constants.ROLE_VIEWER
                        or value & value - 1 != 0
                        and value != 1
                    ):
                        raise Exception(_("Invalid role"))
                    if vals['value'] == 'true':
                        user.role |= value
                    elif vals['value'] == 'false':
                        if (
                            value == constants.ROLE_ADMIN
                            and not ub.session.query(ub.User)
                            .filter(
                                ub.User.role.op('&')(constants.ROLE_ADMIN)
                                == constants.ROLE_ADMIN,
                                ub.User.id != user.id,
                            )
                            .count()
                        ):
                            return Response(
                                json.dumps([{'type': "danger",
                                             'message':_(u"No admin user remaining, can't remove admin role",
                                                         nick=user.name)}]), mimetype='application/json')
                        user.role &= ~value
                    else:
                        raise Exception(_("Value has to be true or false"))
                elif param.startswith('sidebar'):
                    value = int(vals['field_index'])
                    if user.name == "Guest" and value == constants.SIDEBAR_READ_AND_UNREAD:
                        raise Exception(_("Guest can't have this view"))
                    if (
                        value <= 0
                        or value > constants.SIDEBAR_LIST
                        or value & value - 1 != 0
                        and value != 1
                    ):
                        raise Exception(_("Invalid view"))
                    if vals['value'] == 'true':
                        user.sidebar_view |= value
                    elif vals['value'] == 'false':
                        user.sidebar_view &= ~value
                    else:
                        raise Exception(_("Value has to be true or false"))
                elif param == 'locale':
                    if user.name == "Guest":
                        raise Exception(_("Guest's Locale is determined automatically and can't be set"))
                    if vals['value'] in _BABEL_TRANSLATIONS:
                        user.locale = vals['value']
                    else:
                        raise Exception(_("No Valid Locale Given"))
                elif param == 'default_language':
                    languages = calibre_db.session.query(db.Languages) \
                        .join(db.books_languages_link) \
                        .join(db.Books) \
                        .filter(calibre_db.common_filters()) \
                        .group_by(text('books_languages_link.lang_code')).all()
                    lang_codes = [lang.lang_code for lang in languages] + ["all"]
                    if vals['value'] in lang_codes:
                        user.default_language = vals['value']
                    else:
                        raise Exception(_("No Valid Book Language Given"))
                else:
                    return _("Parameter not found"), 400
        except Exception as ex:
            log.debug_or_exception(ex)
            return str(ex), 400
    ub.session_commit()
    return ""


@admi.route("/ajax/user_table_settings", methods=['POST'])
@login_required
@admin_required
def update_table_settings():
    current_user.view_settings['useredit'] = json.loads(request.data)
    try:
        try:
            flag_modified(current_user, "view_settings")
        except AttributeError:
            pass
        ub.session.commit()
    except (InvalidRequestError, OperationalError):
        log.error(f"Invalid request received: {request}")
        return "Invalid request", 400
    return ""

def check_valid_read_column(column):
    return bool(
        column == "0"
        or calibre_db.session.query(db.Custom_Columns)
        .filter(db.Custom_Columns.id == column)
        .filter(
            and_(
                db.Custom_Columns.datatype == 'bool',
                db.Custom_Columns.mark_for_delete == 0,
            )
        )
        .all()
    )

def check_valid_restricted_column(column):
    return bool(
        column == "0"
        or calibre_db.session.query(db.Custom_Columns)
        .filter(db.Custom_Columns.id == column)
        .filter(
            and_(
                db.Custom_Columns.datatype == 'text',
                db.Custom_Columns.mark_for_delete == 0,
            )
        )
        .all()
    )



@admi.route("/admin/viewconfig", methods=["POST"])
@login_required
@admin_required
def update_view_configuration():
    to_save = request.form.to_dict()

    # _config_string = lambda x: config.set_from_dictionary(to_save, x, lambda y: y.strip() if y else y)
    # _config_int = lambda x: config.set_from_dictionary(to_save, x, int)

    _config_string(to_save, "config_calibre_web_title")
    _config_string(to_save, "config_columns_to_ignore")
    if _config_string(to_save, "config_title_regex"):
        calibre_db.update_title_sort(config)

    if not check_valid_read_column(to_save.get("config_read_column", "0")):
        flash(_(u"Invalid Read Column"), category="error")
        log.debug("Invalid Read column")
        return view_configuration()
    _config_int(to_save, "config_read_column")

    if not check_valid_restricted_column(to_save.get("config_restricted_column", "0")):
        flash(_(u"Invalid Restricted Column"), category="error")
        log.debug("Invalid Restricted Column")
        return view_configuration()
    _config_int(to_save, "config_restricted_column")

    _config_int(to_save, "config_theme")
    _config_int(to_save, "config_random_books")
    _config_int(to_save, "config_books_per_page")
    _config_int(to_save, "config_authors_max")


    config.config_default_role = constants.selected_roles(to_save)
    config.config_default_role &= ~constants.ROLE_ANONYMOUS

    config.config_default_show = sum(int(k[5:]) for k in to_save if k.startswith('show_'))
    if "Show_detail_random" in to_save:
        config.config_default_show |= constants.DETAIL_RANDOM

    config.save()
    flash(_(u"Calibre-Web configuration updated"), category="success")
    log.debug("Calibre-Web configuration updated")
    before_request()

    return view_configuration()


@admi.route("/ajax/loaddialogtexts/<element_id>", methods=['POST'])
@login_required
def load_dialogtexts(element_id):
    texts = {"header": "", "main": "", "valid": 1}
    if element_id == "config_delete_kobo_token":
        texts["main"] = _('Do you really want to delete the Kobo Token?')
    elif element_id == "btndeletedomain":
        texts["main"] = _('Do you really want to delete this domain?')
    elif element_id == "btndeluser":
        texts["main"] = _('Do you really want to delete this user?')
    elif element_id == "delete_shelf":
        texts["main"] = _('Are you sure you want to delete this shelf?')
    elif element_id == "select_locale":
        texts["main"] = _('Are you sure you want to change locales of selected user(s)?')
    elif element_id == "select_default_language":
        texts["main"] = _('Are you sure you want to change visible book languages for selected user(s)?')
    elif element_id == "role":
        texts["main"] = _('Are you sure you want to change the selected role for the selected user(s)?')
    elif element_id == "restrictions":
        texts["main"] = _('Are you sure you want to change the selected restrictions for the selected user(s)?')
    elif element_id == "sidebar_view":
        texts["main"] = _('Are you sure you want to change the selected visibility restrictions for the selected user(s)?')
    elif element_id == "kobo_only_shelves_sync":
        texts["main"] = _('Are you sure you want to change shelf sync behavior for the selected user(s)?')
    elif element_id == "db_submit":
        texts["main"] = _('Are you sure you want to change Calibre libray location?')
    return json.dumps(texts)


@admi.route("/ajax/editdomain/<int:allow>", methods=['POST'])
@login_required
@admin_required
def edit_domain(allow):
    # POST /post
    # name:  'username',  //name of field (column in db)
    # pk:    1            //primary key (record id)
    # value: 'superuser!' //new value
    vals = request.form.to_dict()
    answer = ub.session.query(ub.Registration).filter(ub.Registration.id == vals['pk']).first()
    answer.domain = vals['value'].replace('*', '%').replace('?', '_').lower()
    return ub.session_commit(f"Registering Domains edited {answer.domain}")


@admi.route("/ajax/adddomain/<int:allow>", methods=['POST'])
@login_required
@admin_required
def add_domain(allow):
    domain_name = request.form.to_dict()['domainname'].replace('*', '%').replace('?', '_').lower()
    check = ub.session.query(ub.Registration).filter(ub.Registration.domain == domain_name)\
        .filter(ub.Registration.allow == allow).first()
    if not check:
        new_domain = ub.Registration(domain=domain_name, allow=allow)
        ub.session.add(new_domain)
        ub.session_commit(f"Registering Domains added {domain_name}")
    return ""


@admi.route("/ajax/deletedomain", methods=['POST'])
@login_required
@admin_required
def delete_domain():
    try:
        domain_id = request.form.to_dict()['domainid'].replace('*', '%').replace('?', '_').lower()
        ub.session.query(ub.Registration).filter(ub.Registration.id == domain_id).delete()
        ub.session_commit(f"Registering Domains deleted {domain_id}")
        # If last domain was deleted, add all domains by default
        if not ub.session.query(ub.Registration).filter(ub.Registration.allow == 1).count():
            new_domain = ub.Registration(domain="%.%", allow=1)
            ub.session.add(new_domain)
            ub.session_commit("Last Registering Domain deleted, added *.* as default")
    except KeyError:
        pass
    return ""


@admi.route("/ajax/domainlist/<int:allow>")
@login_required
@admin_required
def list_domain(allow):
    answer = ub.session.query(ub.Registration).filter(ub.Registration.allow == allow).all()
    json_dumps = json.dumps([{"domain": r.domain.replace('%', '*').replace('_', '?'), "id": r.id} for r in answer])
    js = json.dumps(json_dumps.replace('"', "'")).lstrip('"').strip('"')
    response = make_response(js.replace("'", '"'))
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@admi.route("/ajax/editrestriction/<int:res_type>", defaults={"user_id": 0}, methods=['POST'])
@admi.route("/ajax/editrestriction/<int:res_type>/<int:user_id>", methods=['POST'])
@login_required
@admin_required
def edit_restriction(res_type, user_id):
    element = request.form.to_dict()
    if element['id'].startswith('a'):
        if res_type == 0:  # Tags as template
            elementlist = config.list_allowed_tags()
            elementlist[int(element['id'][1:])] = element['Element']
            config.config_allowed_tags = ','.join(elementlist)
            config.save()
        if res_type == 1:  # CustomC
            elementlist = config.list_allowed_column_values()
            elementlist[int(element['id'][1:])] = element['Element']
            config.config_allowed_column_value = ','.join(elementlist)
            config.save()
        if res_type == 2:  # Tags per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_allowed_tags()
            elementlist[int(element['id'][1:])] = element['Element']
            usr.allowed_tags = ','.join(elementlist)
            ub.session_commit(
                f"Changed allowed tags of user {usr.name} to {usr.allowed_tags}"
            )

        if res_type == 3:  # CColumn per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_allowed_column_values()
            elementlist[int(element['id'][1:])] = element['Element']
            usr.allowed_column_value = ','.join(elementlist)
            ub.session_commit(
                f"Changed allowed columns of user {usr.name} to {usr.allowed_column_value}"
            )

    if element['id'].startswith('d'):
        if res_type == 0:  # Tags as template
            elementlist = config.list_denied_tags()
            elementlist[int(element['id'][1:])] = element['Element']
            config.config_denied_tags = ','.join(elementlist)
            config.save()
        if res_type == 1:  # CustomC
            elementlist = config.list_denied_column_values()
            elementlist[int(element['id'][1:])] = element['Element']
            config.config_denied_column_value = ','.join(elementlist)
            config.save()
        if res_type == 2:  # Tags per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_denied_tags()
            elementlist[int(element['id'][1:])] = element['Element']
            usr.denied_tags = ','.join(elementlist)
            ub.session_commit(
                f"Changed denied tags of user {usr.name} to {usr.denied_tags}"
            )

        if res_type == 3:  # CColumn per user
            if isinstance(user_id, int):
                usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
            else:
                usr = current_user
            elementlist = usr.list_denied_column_values()
            elementlist[int(element['id'][1:])] = element['Element']
            usr.denied_column_value = ','.join(elementlist)
            ub.session_commit(
                f"Changed denied columns of user {usr.name} to {usr.denied_column_value}"
            )

    return ""


def restriction_addition(element, list_func):
    elementlist = list_func()
    if elementlist == ['']:
        elementlist = []
    if element['add_element'] not in elementlist:
        elementlist += [element['add_element']]
    return ','.join(elementlist)


def restriction_deletion(element, list_func):
    elementlist = list_func()
    if element['Element'] in elementlist:
        elementlist.remove(element['Element'])
    return ','.join(elementlist)


def prepare_tags(user, action, tags_name, id_list):
    if "tags" in tags_name:
        tags = calibre_db.session.query(db.Tags).filter(db.Tags.id.in_(id_list)).all()
        if not tags:
            raise Exception(_("Tag not found"))
        new_tags_list = [x.name for x in tags]
    else:
        tags = calibre_db.session.query(db.cc_classes[config.config_restricted_column])\
            .filter(db.cc_classes[config.config_restricted_column].id.in_(id_list)).all()
        new_tags_list = [x.value for x in tags]
    saved_tags_list = user.__dict__[tags_name].split(",") if len(user.__dict__[tags_name]) else []
    if action == "remove":
        saved_tags_list = [x for x in saved_tags_list if x not in new_tags_list]
    elif action == "add":
        saved_tags_list.extend(x for x in new_tags_list if x not in saved_tags_list)
    else:
        raise Exception(_("Invalid Action"))
    return ",".join(saved_tags_list)


@admi.route("/ajax/addrestriction/<int:res_type>", defaults={"user_id": 0}, methods=['POST'])
@admi.route("/ajax/addrestriction/<int:res_type>/<int:user_id>", methods=['POST'])
@login_required
@admin_required
def add_restriction(res_type, user_id):
    element = request.form.to_dict()
    if res_type == 0:  # Tags as template
        if 'submit_allow' in element:
            config.config_allowed_tags = restriction_addition(element, config.list_allowed_tags)
            config.save()
        elif 'submit_deny' in element:
            config.config_denied_tags = restriction_addition(element, config.list_denied_tags)
            config.save()
    if res_type == 1:  # CCustom as template
        if 'submit_allow' in element:
            config.config_allowed_column_value = restriction_addition(element, config.list_denied_column_values)
            config.save()
        elif 'submit_deny' in element:
            config.config_denied_column_value = restriction_addition(element, config.list_allowed_column_values)
            config.save()
    if res_type == 2:  # Tags per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if 'submit_allow' in element:
            usr.allowed_tags = restriction_addition(element, usr.list_allowed_tags)
            ub.session_commit(
                f"Changed allowed tags of user {usr.name} to {usr.list_allowed_tags()}"
            )

        elif 'submit_deny' in element:
            usr.denied_tags = restriction_addition(element, usr.list_denied_tags)
            ub.session_commit(
                f"Changed denied tags of user {usr.name} to {usr.list_denied_tags()}"
            )

    if res_type == 3:  # CustomC per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if 'submit_allow' in element:
            usr.allowed_column_value = restriction_addition(element, usr.list_allowed_column_values)
            ub.session_commit(
                f"Changed allowed columns of user {usr.name} to {usr.list_allowed_column_values()}"
            )

        elif 'submit_deny' in element:
            usr.denied_column_value = restriction_addition(element, usr.list_denied_column_values)
            ub.session_commit(
                f"Changed denied columns of user {usr.name} to {usr.list_denied_column_values()}"
            )

    return ""


@admi.route("/ajax/deleterestriction/<int:res_type>", defaults={"user_id": 0}, methods=['POST'])
@admi.route("/ajax/deleterestriction/<int:res_type>/<int:user_id>", methods=['POST'])
@login_required
@admin_required
def delete_restriction(res_type, user_id):
    element = request.form.to_dict()
    if res_type == 0:  # Tags as template
        if element['id'].startswith('a'):
            config.config_allowed_tags = restriction_deletion(element, config.list_allowed_tags)
            config.save()
        elif element['id'].startswith('d'):
            config.config_denied_tags = restriction_deletion(element, config.list_denied_tags)
            config.save()
    elif res_type == 1:  # CustomC as template
        if element['id'].startswith('a'):
            config.config_allowed_column_value = restriction_deletion(element, config.list_allowed_column_values)
            config.save()
        elif element['id'].startswith('d'):
            config.config_denied_column_value = restriction_deletion(element, config.list_denied_column_values)
            config.save()
    elif res_type == 2:  # Tags per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if element['id'].startswith('a'):
            usr.allowed_tags = restriction_deletion(element, usr.list_allowed_tags)
            ub.session_commit("Deleted allowed tags of user {}: {}".format(usr.name, usr.list_allowed_tags))
        elif element['id'].startswith('d'):
            usr.denied_tags = restriction_deletion(element, usr.list_denied_tags)
            ub.session_commit("Deleted denied tags of user {}: {}".format(usr.name, usr.list_allowed_tags))
    elif res_type == 3:  # Columns per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()
        else:
            usr = current_user
        if element['id'].startswith('a'):
            usr.allowed_column_value = restriction_deletion(element, usr.list_allowed_column_values)
            ub.session_commit("Deleted allowed columns of user {}: {}".format(usr.name,
                                                                              usr.list_allowed_column_values))

        elif element['id'].startswith('d'):
            usr.denied_column_value = restriction_deletion(element, usr.list_denied_column_values)
            ub.session_commit("Deleted denied columns of user {}: {}".format(usr.name,
                                                                             usr.list_denied_column_values))
    return ""


@admi.route("/ajax/listrestriction/<int:res_type>", defaults={"user_id": 0})
@admi.route("/ajax/listrestriction/<int:res_type>/<int:user_id>")
@login_required
@admin_required
def list_restriction(res_type, user_id):
    if res_type == 0:   # Tags as template
        restrict = [{'Element': x, 'type':_('Deny'), 'id': 'd'+str(i) }
                    for i,x in enumerate(config.list_denied_tags()) if x != '']
        allow = [{'Element': x, 'type': _('Allow'), 'id': 'a'+str(i)}
                 for i, x in enumerate(config.list_allowed_tags()) if x != '']
        json_dumps = restrict + allow
    elif res_type == 1:  # CustomC as template
        restrict = [{'Element': x, 'type': _('Deny'), 'id': 'd'+str(i)}
                    for i, x in enumerate(config.list_denied_column_values()) if x != '']
        allow = [{'Element': x, 'type': _('Allow'), 'id': 'a'+str(i)}
                 for i, x in enumerate(config.list_allowed_column_values()) if x != '']
        json_dumps = restrict + allow
    elif res_type == 2:  # Tags per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == user_id).first()
        else:
            usr = current_user
        restrict = [{'Element': x, 'type': _('Deny'), 'id': 'd'+str(i)}
                    for i, x in enumerate(usr.list_denied_tags()) if x != '']
        allow = [{'Element': x, 'type': _('Allow'), 'id': 'a'+str(i)}
                 for i, x in enumerate(usr.list_allowed_tags()) if x != '']
        json_dumps = restrict + allow
    elif res_type == 3:  # CustomC per user
        if isinstance(user_id, int):
            usr = ub.session.query(ub.User).filter(ub.User.id == user_id).first()
        else:
            usr = current_user
        restrict = [{'Element': x, 'type': _('Deny'), 'id': 'd'+str(i)}
                    for i, x in enumerate(usr.list_denied_column_values()) if x != '']
        allow = [{'Element': x, 'type': _('Allow'), 'id': 'a'+str(i)}
                 for i, x in enumerate(usr.list_allowed_column_values()) if x != '']
        json_dumps = restrict + allow
    else:
        json_dumps = ""
    js = json.dumps(json_dumps)
    response = make_response(js.replace("'", '"'))
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@admi.route("/ajax/pathchooser/")
@login_required
@admin_required
def ajax_pathchooser():
    return pathchooser()


def pathchooser():
    browse_for = "folder"
    folder_only = request.args.get('folder', False) == "true"
    file_filter = request.args.get('filter', "")
    path = os.path.normpath(request.args.get('path', ""))

    if os.path.isfile(path):
        oldfile = path
        path = os.path.dirname(path)
    else:
        oldfile = ""

    absolute = False

    if os.path.isdir(path):
        # if os.path.isabs(path):
        cwd = os.path.realpath(path)
        absolute = True
        # else:
        #    cwd = os.path.relpath(path)
    else:
        cwd = os.getcwd()

    cwd = os.path.normpath(os.path.realpath(cwd))
    parentdir = os.path.dirname(cwd)
    if not absolute:
        if os.path.realpath(cwd) == os.path.realpath("/"):
            cwd = os.path.relpath(cwd)
        else:
            cwd = os.path.relpath(cwd) + os.path.sep
        parentdir = os.path.relpath(parentdir) + os.path.sep

    if os.path.realpath(cwd) == os.path.realpath("/"):
        parentdir = ""

    try:
        folders = os.listdir(cwd)
    except Exception:
        folders = []

    files = []
    for f in folders:
        try:
            data = {"name": f, "fullpath": os.path.join(cwd, f)}
            data["sort"] = data["fullpath"].lower()
        except Exception:
            continue

        if os.path.isfile(os.path.join(cwd, f)):
            if folder_only:
                continue
            if file_filter != "" and file_filter != f:
                continue
            data["type"] = "file"
            data["size"] = os.path.getsize(os.path.join(cwd, f))

            power = 0
            while (data["size"] >> 10) > 0.3:
                power += 1
                data["size"] >>= 10
            units = ("", "K", "M", "G", "T")
            data["size"] = str(data["size"]) + " " + units[power] + "Byte"
        else:
            data["type"] = "dir"
            data["size"] = ""

        files.append(data)

    files = sorted(files, key=operator.itemgetter("type", "sort"))

    context = {
        "cwd": cwd,
        "files": files,
        "parentdir": parentdir,
        "type": browse_for,
        "oldfile": oldfile,
        "absolute": absolute,
    }
    return json.dumps(context)


def _config_int(to_save, x, func=int):
    return config.set_from_dictionary(to_save, x, func)


def _config_checkbox(to_save, x):
    return config.set_from_dictionary(to_save, x, lambda y: y == "on", False)


def _config_checkbox_int(to_save, x):
    return config.set_from_dictionary(to_save, x, lambda y: 1 if (y == "on") else 0, 0)


def _config_string(to_save, x):
    return config.set_from_dictionary(to_save, x, lambda y: y.strip() if y else y)


def _configuration_gdrive_helper(to_save):
    gdrive_error = None
    if to_save.get("config_use_google_drive"):
        gdrive_secrets = {}

        if not os.path.isfile(gdriveutils.SETTINGS_YAML):
            config.config_use_google_drive = False

        if gdrive_support:
            gdrive_error = gdriveutils.get_error_text(gdrive_secrets)
        if "config_use_google_drive" in to_save and not config.config_use_google_drive and not gdrive_error:
            with open(gdriveutils.CLIENT_SECRETS, 'r') as settings:
                gdrive_secrets = json.load(settings)['web']
            if not gdrive_secrets:
                return _configuration_result(_('client_secrets.json Is Not Configured For Web Application'))
            gdriveutils.update_settings(
                                gdrive_secrets['client_id'],
                                gdrive_secrets['client_secret'],
                                gdrive_secrets['redirect_uris'][0]
                            )

    # always show google drive settings, but in case of error deny support
    new_gdrive_value = (not gdrive_error) and ("config_use_google_drive" in to_save)
    if config.config_use_google_drive and not new_gdrive_value:
        config.config_google_drive_watch_changes_response = {}
    config.config_use_google_drive = new_gdrive_value
    if _config_string(to_save, "config_google_drive_folder"):
        gdriveutils.deleteDatabaseOnChange()
    return gdrive_error


def _configuration_oauth_helper(to_save):
    active_oauths = 0
    reboot_required = False
    for element in oauthblueprints:
        if to_save["config_" + str(element['id']) + "_oauth_client_id"] != element['oauth_client_id'] \
            or to_save["config_" + str(element['id']) + "_oauth_client_secret"] != element['oauth_client_secret']:
            reboot_required = True
            element['oauth_client_id'] = to_save["config_" + str(element['id']) + "_oauth_client_id"]
            element['oauth_client_secret'] = to_save["config_" + str(element['id']) + "_oauth_client_secret"]
        if to_save["config_" + str(element['id']) + "_oauth_client_id"] \
            and to_save["config_" + str(element['id']) + "_oauth_client_secret"]:
            active_oauths += 1
            element["active"] = 1
        else:
            element["active"] = 0
        ub.session.query(ub.OAuthProvider).filter(ub.OAuthProvider.id == element['id']).update(
            {"oauth_client_id": to_save["config_" + str(element['id']) + "_oauth_client_id"],
             "oauth_client_secret": to_save["config_" + str(element['id']) + "_oauth_client_secret"],
             "active": element["active"]})
    return reboot_required


def _configuration_logfile_helper(to_save):
    reboot_required = False
    reboot_required |= _config_int(to_save, "config_log_level")
    reboot_required |= _config_string(to_save, "config_logfile")
    if not logger.is_valid_logfile(config.config_logfile):
        return reboot_required, \
               _configuration_result(_('Logfile Location is not Valid, Please Enter Correct Path'))

    reboot_required |= _config_checkbox_int(to_save, "config_access_log")
    reboot_required |= _config_string(to_save, "config_access_logfile")
    if not logger.is_valid_logfile(config.config_access_logfile):
        return reboot_required, \
               _configuration_result(_('Access Logfile Location is not Valid, Please Enter Correct Path'))
    return reboot_required, None


def _configuration_ldap_helper(to_save):
    reboot_required = False
    reboot_required |= _config_string(to_save, "config_ldap_provider_url")
    reboot_required |= _config_int(to_save, "config_ldap_port")
    reboot_required |= _config_int(to_save, "config_ldap_authentication")
    reboot_required |= _config_string(to_save, "config_ldap_dn")
    reboot_required |= _config_string(to_save, "config_ldap_serv_username")
    reboot_required |= _config_string(to_save, "config_ldap_user_object")
    reboot_required |= _config_string(to_save, "config_ldap_group_object_filter")
    reboot_required |= _config_string(to_save, "config_ldap_group_members_field")
    reboot_required |= _config_string(to_save, "config_ldap_member_user_object")
    reboot_required |= _config_checkbox(to_save, "config_ldap_openldap")
    reboot_required |= _config_int(to_save, "config_ldap_encryption")
    reboot_required |= _config_string(to_save, "config_ldap_cacert_path")
    reboot_required |= _config_string(to_save, "config_ldap_cert_path")
    reboot_required |= _config_string(to_save, "config_ldap_key_path")
    _config_string(to_save, "config_ldap_group_name")
    if to_save.get("config_ldap_serv_password", "") != "":
        reboot_required |= 1
        config.set_from_dictionary(to_save, "config_ldap_serv_password", base64.b64encode, encode='UTF-8')
    config.save()

    if not config.config_ldap_provider_url \
        or not config.config_ldap_port \
        or not config.config_ldap_dn \
        or not config.config_ldap_user_object:
        return reboot_required, _configuration_result(_('Please Enter a LDAP Provider, '
                                                        'Port, DN and User Object Identifier'))

    if config.config_ldap_authentication > constants.LDAP_AUTH_ANONYMOUS:
        if config.config_ldap_authentication > constants.LDAP_AUTH_UNAUTHENTICATE:
            if not config.config_ldap_serv_username or not bool(config.config_ldap_serv_password):
                return reboot_required, _configuration_result(_('Please Enter a LDAP Service Account and Password'))
        else:
            if not config.config_ldap_serv_username:
                return reboot_required, _configuration_result(_('Please Enter a LDAP Service Account'))

    if config.config_ldap_group_object_filter:
        if config.config_ldap_group_object_filter.count("%s") != 1:
            return reboot_required, \
                   _configuration_result(_('LDAP Group Object Filter Needs to Have One "%s" Format Identifier'))
        if config.config_ldap_group_object_filter.count("(") != config.config_ldap_group_object_filter.count(")"):
            return reboot_required, _configuration_result(_('LDAP Group Object Filter Has Unmatched Parenthesis'))

    if config.config_ldap_user_object.count("%s") != 1:
        return reboot_required, \
               _configuration_result(_('LDAP User Object Filter needs to Have One "%s" Format Identifier'))
    if config.config_ldap_user_object.count("(") != config.config_ldap_user_object.count(")"):
        return reboot_required, _configuration_result(_('LDAP User Object Filter Has Unmatched Parenthesis'))

    if to_save.get("ldap_import_user_filter") == '0':
        config.config_ldap_member_user_object = ""
    else:
        if config.config_ldap_member_user_object.count("%s") != 1:
            return reboot_required, \
                   _configuration_result(_('LDAP Member User Filter needs to Have One "%s" Format Identifier'))
        if config.config_ldap_member_user_object.count("(") != config.config_ldap_member_user_object.count(")"):
            return reboot_required, _configuration_result(_('LDAP Member User Filter Has Unmatched Parenthesis'))

    if config.config_ldap_cacert_path or config.config_ldap_cert_path or config.config_ldap_key_path:
        if not (os.path.isfile(config.config_ldap_cacert_path) and
                os.path.isfile(config.config_ldap_cert_path) and
                os.path.isfile(config.config_ldap_key_path)):
            return reboot_required, \
                   _configuration_result(_('LDAP CACertificate, Certificate or Key Location is not Valid, '
                                           'Please Enter Correct Path'))
    return reboot_required, None


@admi.route("/ajax/simulatedbchange", methods=['POST'])
@login_required
@admin_required
def simulatedbchange():
    db_change, db_valid = _db_simulate_change()
    return Response(json.dumps({"change": db_change, "valid": db_valid}), mimetype='application/json')


def _db_simulate_change():
    param = request.form.to_dict()
    to_save = {}
    to_save['config_calibre_dir'] = re.sub(r'[\\/]metadata\.db$',
                                           '',
                                           param['config_calibre_dir'],
                                           flags=re.IGNORECASE).strip()
    db_change = config.config_calibre_dir != to_save["config_calibre_dir"] and config.config_calibre_dir
    db_valid = calibre_db.check_valid_db(to_save["config_calibre_dir"], ub.app_DB_path)
    return db_change, db_valid


def _db_configuration_update_helper():
    db_change = False
    to_save = request.form.to_dict()
    gdrive_error = None

    to_save['config_calibre_dir'] = re.sub(r'[\\/]metadata\.db$',
                                           '',
                                           to_save['config_calibre_dir'],
                                           flags=re.IGNORECASE)
    try:
        db_change, db_valid = _db_simulate_change()

        # gdrive_error drive setup
        gdrive_error = _configuration_gdrive_helper(to_save)
    except (OperationalError, InvalidRequestError):
        ub.session.rollback()
        log.error("Settings DB is not Writeable")
        _db_configuration_result(_("Settings DB is not Writeable"), gdrive_error)
    try:
        metadata_db = os.path.join(to_save['config_calibre_dir'], "metadata.db")
        if config.config_use_google_drive and is_gdrive_ready() and not os.path.exists(metadata_db):
            gdriveutils.downloadFile(None, "metadata.db", metadata_db)
            db_change = True
    except Exception as ex:
        return _db_configuration_result('{}'.format(ex), gdrive_error)

    if db_change or not db_valid or not config.db_configured:
        if not calibre_db.setup_db(to_save['config_calibre_dir'], ub.app_DB_path):
            return _db_configuration_result(_('DB Location is not Valid, Please Enter Correct Path'),
                                            gdrive_error)
        _config_string(to_save, "config_calibre_dir")
        calibre_db.update_config(config)
        if not os.access(os.path.join(config.config_calibre_dir, "metadata.db"), os.W_OK):
            flash(_(u"DB is not Writeable"), category="warning")
            # warning = {'type': "warning", 'message': _(u"DB is not Writeable")}
    config.save()
    return _db_configuration_result(None, gdrive_error)

def _configuration_update_helper():
    reboot_required = False
    to_save = request.form.to_dict()
    try:
        reboot_required |= _config_int(to_save, "config_port")

        reboot_required |= _config_string(to_save, "config_keyfile")
        if config.config_keyfile and not os.path.isfile(config.config_keyfile):
            return _configuration_result(_('Keyfile Location is not Valid, Please Enter Correct Path'))

        reboot_required |= _config_string(to_save, "config_certfile")
        if config.config_certfile and not os.path.isfile(config.config_certfile):
            return _configuration_result(_('Certfile Location is not Valid, Please Enter Correct Path'))

        _config_checkbox_int(to_save, "config_uploading")
        # Reboot on config_anonbrowse with enabled ldap, as decoraters are changed in this case
        reboot_required |= (_config_checkbox_int(to_save, "config_anonbrowse")
                             and config.config_login_type == constants.LOGIN_LDAP)
        _config_checkbox_int(to_save, "config_public_reg")
        _config_checkbox_int(to_save, "config_register_email")
        reboot_required |= _config_checkbox_int(to_save, "config_kobo_sync")
        _config_int(to_save, "config_external_port")
        _config_checkbox_int(to_save, "config_kobo_proxy")

        if "config_upload_formats" in to_save:
            to_save["config_upload_formats"] = ','.join(
                helper.uniq([x.lstrip().rstrip().lower() for x in to_save["config_upload_formats"].split(',')]))
            _config_string(to_save, "config_upload_formats")
            constants.EXTENSIONS_UPLOAD = config.config_upload_formats.split(',')

        _config_string(to_save, "config_calibre")
        _config_string(to_save, "config_converterpath")
        _config_string(to_save, "config_kepubifypath")

        reboot_required |= _config_int(to_save, "config_login_type")

        # LDAP configurator
        if config.config_login_type == constants.LOGIN_LDAP:
            reboot, message = _configuration_ldap_helper(to_save)
            if message:
                return message
            reboot_required |= reboot

        # Remote login configuration
        _config_checkbox(to_save, "config_remote_login")
        if not config.config_remote_login:
            ub.session.query(ub.RemoteAuthToken).filter(ub.RemoteAuthToken.token_type == 0).delete()

        # Goodreads configuration
        _config_checkbox(to_save, "config_use_goodreads")
        _config_string(to_save, "config_goodreads_api_key")
        _config_string(to_save, "config_goodreads_api_secret")
        if services.goodreads_support:
            services.goodreads_support.connect(config.config_goodreads_api_key,
                                               config.config_goodreads_api_secret,
                                               config.config_use_goodreads)

        _config_int(to_save, "config_updatechannel")

        # Reverse proxy login configuration
        _config_checkbox(to_save, "config_allow_reverse_proxy_header_login")
        _config_string(to_save, "config_reverse_proxy_login_header_name")

        # OAuth configuration
        if config.config_login_type == constants.LOGIN_OAUTH:
            reboot_required |= _configuration_oauth_helper(to_save)

        reboot, message = _configuration_logfile_helper(to_save)
        if message:
            return message
        reboot_required |= reboot
        # Rarfile Content configuration
        _config_string(to_save, "config_rarfile_location")
        if "config_rarfile_location" in to_save:
            unrar_status = helper.check_unrar(config.config_rarfile_location)
            if unrar_status:
                return _configuration_result(unrar_status)
    except (OperationalError, InvalidRequestError):
        ub.session.rollback()
        log.error("Settings DB is not Writeable")
        _configuration_result(_("Settings DB is not Writeable"))

    config.save()
    if reboot_required:
        web_server.stop(True)

    return _configuration_result(None, reboot_required)

def _configuration_result(error_flash=None, reboot=False):
    resp = {}
    if error_flash:
        log.error(error_flash)
        config.load()
        resp['result'] = [{'type': "danger", 'message': error_flash}]
    else:
        resp['result'] = [{'type': "success", 'message':_(u"Calibre-Web configuration updated")}]
    resp['reboot'] = reboot
    resp['config_upload']= config.config_upload_formats
    return Response(json.dumps(resp), mimetype='application/json')


def _db_configuration_result(error_flash=None, gdrive_error=None):
    gdrive_authenticate = not is_gdrive_ready()
    gdrivefolders = []
    if not gdrive_error and config.config_use_google_drive:
        gdrive_error = gdriveutils.get_error_text()
    if gdrive_error and gdrive_support:
        log.error(gdrive_error)
        gdrive_error = _(gdrive_error)
        flash(gdrive_error, category="error")
    else:
        if not gdrive_authenticate and gdrive_support:
            gdrivefolders = gdriveutils.listRootFolders()
    if error_flash:
        log.error(error_flash)
        config.load()
        flash(error_flash, category="error")

    return render_title_template("config_db.html",
                                 config=config,
                                 show_authenticate_google_drive=gdrive_authenticate,
                                 gdriveError=gdrive_error,
                                 gdrivefolders=gdrivefolders,
                                 feature_support=feature_support,
                                 title=_(u"Database Configuration"), page="dbconfig")


def _handle_new_user(to_save, content, languages, translations, kobo_support):
    content.default_language = to_save["default_language"]
    content.locale = to_save.get("locale", content.locale)

    content.sidebar_view = sum(int(key[5:]) for key in to_save if key.startswith('show_'))
    if "show_detail_random" in to_save:
        content.sidebar_view |= constants.DETAIL_RANDOM

    content.role = constants.selected_roles(to_save)
    content.password = generate_password_hash(to_save["password"])
    try:
        if not to_save["name"] or not to_save["email"] or not to_save["password"]:
            log.info("Missing entries on new user")
            raise Exception(_(u"Please fill out all fields!"))
        content.email = check_email(to_save["email"])
        # Query User name, if not existing, change
        content.name = check_username(to_save["name"])
        if to_save.get("kindle_mail"):
            content.kindle_mail = valid_email(to_save["kindle_mail"])
        if config.config_public_reg and not check_valid_domain(content.email):
            log.info("E-mail: {} for new user is not from valid domain".format(content.email))
            raise Exception(_(u"E-mail is not from valid domain"))
    except Exception as ex:
        flash(str(ex), category="error")
        return render_title_template("user_edit.html", new_user=1, content=content, translations=translations,
                                     languages=languages, title=_(u"Add new user"), page="newuser",
                                     kobo_support=kobo_support, registered_oauth=oauth_check)
    try:
        content.allowed_tags = config.config_allowed_tags
        content.denied_tags = config.config_denied_tags
        content.allowed_column_value = config.config_allowed_column_value
        content.denied_column_value = config.config_denied_column_value
        content.kobo_only_shelves_sync = 0  # No default value for kobo sync shelf setting
        ub.session.add(content)
        ub.session.commit()
        flash(_(u"User '%(user)s' created", user=content.name), category="success")
        log.debug("User {} created".format(content.name))
        return redirect(url_for('admin.admin'))
    except IntegrityError:
        ub.session.rollback()
        log.error("Found an existing account for {} or {}".format(content.name, content.email))
        flash(_("Found an existing account for this e-mail address or name."), category="error")
    except OperationalError:
        ub.session.rollback()
        log.error("Settings DB is not Writeable")
        flash(_("Settings DB is not Writeable"), category="error")

def _delete_user(content):
    if ub.session.query(ub.User).filter(ub.User.role.op('&')(constants.ROLE_ADMIN) == constants.ROLE_ADMIN,
                                        ub.User.id != content.id).count():
        if content.name != "Guest":
            ub.session.query(ub.User).filter(ub.User.id == content.id).delete()
            ub.session_commit()
            log.info(u"User {} deleted".format(content.name))
            return(_(u"User '%(nick)s' deleted", nick=content.name))
        else:
            log.warning(_(u"Can't delete Guest User"))
            raise Exception(_(u"Can't delete Guest User"))
    else:
        log.warning(u"No admin user remaining, can't delete user")
        raise Exception(_(u"No admin user remaining, can't delete user"))


def _handle_edit_user(to_save, content, languages, translations, kobo_support):
    if to_save.get("delete"):
        try:
            flash(_delete_user(content), category="success")
        except Exception as ex:
            log.error(ex)
            flash(str(ex), category="error")
        return redirect(url_for('admin.admin'))
    else:
        if not ub.session.query(ub.User).filter(ub.User.role.op('&')(constants.ROLE_ADMIN) == constants.ROLE_ADMIN,
                                                ub.User.id != content.id).count() and 'admin_role' not in to_save:
            log.warning("No admin user remaining, can't remove admin role from {}".format(content.name))
            flash(_("No admin user remaining, can't remove admin role"), category="error")
            return redirect(url_for('admin.admin'))
        if to_save.get("password"):
            content.password = generate_password_hash(to_save["password"])
        anonymous = content.is_anonymous
        content.role = constants.selected_roles(to_save)
        if anonymous:
            content.role |= constants.ROLE_ANONYMOUS
        else:
            content.role &= ~constants.ROLE_ANONYMOUS

        val = [int(k[5:]) for k in to_save if k.startswith('show_')]
        sidebar = get_sidebar_config()
        for element in sidebar:
            value = element['visibility']
            if value in val and not content.check_visibility(value):
                content.sidebar_view |= value
            elif value not in val and content.check_visibility(value):
                content.sidebar_view &= ~value

        if to_save.get("Show_detail_random"):
            content.sidebar_view |= constants.DETAIL_RANDOM
        else:
            content.sidebar_view &= ~constants.DETAIL_RANDOM

        content.kobo_only_shelves_sync = int(to_save.get("kobo_only_shelves_sync") == "on") or 0

        if to_save.get("default_language"):
            content.default_language = to_save["default_language"]
        if to_save.get("locale"):
            content.locale = to_save["locale"]
        try:
            if to_save.get("email", content.email) != content.email:
                content.email = check_email(to_save["email"])
            # Query User name, if not existing, change
            if to_save.get("name", content.name) != content.name:
                if to_save.get("name") == "Guest":
                    raise Exception(_("Guest Name can't be changed"))
                content.name = check_username(to_save["name"])
            if to_save.get("kindle_mail") != content.kindle_mail:
                content.kindle_mail = valid_email(to_save["kindle_mail"]) if to_save["kindle_mail"] else ""
        except Exception as ex:
            log.error(ex)
            flash(str(ex), category="error")
            return render_title_template("user_edit.html",
                                         translations=translations,
                                         languages=languages,
                                         mail_configured=config.get_mail_server_configured(),
                                         kobo_support=kobo_support,
                                         new_user=0,
                                         content=content,
                                         registered_oauth=oauth_check,
                                         title=_(u"Edit User %(nick)s", nick=content.name),
                                         page="edituser")
    try:
        ub.session_commit()
        flash(_(u"User '%(nick)s' updated", nick=content.name), category="success")
    except IntegrityError as ex:
        ub.session.rollback()
        log.error("An unknown error occurred while changing user: {}".format(str(ex)))
        flash(_(u"An unknown error occurred. Please try again later."), category="error")
    except OperationalError:
        ub.session.rollback()
        log.error("Settings DB is not Writeable")
        flash(_("Settings DB is not Writeable"), category="error")
    return ""


@admi.route("/admin/user/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_user():
    content = ub.User()
    languages = calibre_db.speaking_language()
    translations = [LC('en')] + babel.list_translations()
    kobo_support = feature_support['kobo'] and config.config_kobo_sync
    if request.method == "POST":
        to_save = request.form.to_dict()
        _handle_new_user(to_save, content, languages, translations, kobo_support)
    else:
        content.role = config.config_default_role
        content.sidebar_view = config.config_default_show
    return render_title_template("user_edit.html", new_user=1, content=content, translations=translations,
                                 languages=languages, title=_(u"Add new user"), page="newuser",
                                 kobo_support=kobo_support, registered_oauth=oauth_check)


@admi.route("/admin/mailsettings")
@login_required
@admin_required
def edit_mailsettings():
    content = config.get_mail_settings()
    return render_title_template("email_edit.html", content=content, title=_(u"Edit E-mail Server Settings"),
                                 page="mailset", feature_support=feature_support)


@admi.route("/admin/mailsettings", methods=["POST"])
@login_required
@admin_required
def update_mailsettings():
    to_save = request.form.to_dict()
    _config_int(to_save, "mail_server_type")
    if to_save.get("invalidate"):
        config.mail_gmail_token = {}
        try:
            flag_modified(config, "mail_gmail_token")
        except AttributeError:
            pass
    elif to_save.get("gmail"):
        try:
            config.mail_gmail_token = services.gmail.setup_gmail(config.mail_gmail_token)
            flash(_(u"Gmail Account Verification Successful"), category="success")
        except Exception as ex:
            flash(str(ex), category="error")
            log.error(ex)
            return edit_mailsettings()

    else:
        _config_string(to_save, "mail_server")
        _config_int(to_save, "mail_port")
        _config_int(to_save, "mail_use_ssl")
        _config_string(to_save, "mail_login")
        _config_string(to_save, "mail_password")
        _config_string(to_save, "mail_from")
        _config_int(to_save, "mail_size", lambda y: int(y)*1024*1024)
    try:
        config.save()
    except (OperationalError, InvalidRequestError):
        ub.session.rollback()
        log.error("Settings DB is not Writeable")
        flash(_("Settings DB is not Writeable"), category="error")
        return edit_mailsettings()

    if to_save.get("test"):
        if current_user.email:
            result = send_test_mail(current_user.email, current_user.name)
            if result is None:
                flash(_(u"Test e-mail queued for sending to %(email)s, please check Tasks for result",
                        email=current_user.email), category="info")
            else:
                flash(_(u"There was an error sending the Test e-mail: %(res)s", res=result), category="error")
        else:
            flash(_(u"Please configure your e-mail address first..."), category="error")
    else:
        flash(_(u"E-mail server settings updated"), category="success")

    return edit_mailsettings()


@admi.route("/admin/user/<int:user_id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(user_id):
    content = ub.session.query(ub.User).filter(ub.User.id == int(user_id)).first()  # type: ub.User
    if not content or (not config.config_anonbrowse and content.name == "Guest"):
        flash(_(u"User not found"), category="error")
        return redirect(url_for('admin.admin'))
    languages = calibre_db.speaking_language()
    translations = babel.list_translations() + [LC('en')]
    kobo_support = feature_support['kobo'] and config.config_kobo_sync
    if request.method == "POST":
        to_save = request.form.to_dict()
        resp = _handle_edit_user(to_save, content, languages, translations, kobo_support)
        if resp:
            return resp
    return render_title_template("user_edit.html",
                                 translations=translations,
                                 languages=languages,
                                 new_user=0,
                                 content=content,
                                 registered_oauth=oauth_check,
                                 mail_configured=config.get_mail_server_configured(),
                                 kobo_support=kobo_support,
                                 title=_(u"Edit User %(nick)s", nick=content.name),
                                 page="edituser")


@admi.route("/admin/resetpassword/<int:user_id>")
@login_required
@admin_required
def reset_user_password(user_id):
    if current_user is not None and current_user.is_authenticated:
        ret, message = reset_password(user_id)
        if ret == 1:
            log.debug(u"Password for user %s reset", message)
            flash(_(u"Password for user %(user)s reset", user=message), category="success")
        elif ret == 0:
            log.error(u"An unknown error occurred. Please try again later.")
            flash(_(u"An unknown error occurred. Please try again later."), category="error")
        else:
            log.error(u"Please configure the SMTP mail settings first...")
            flash(_(u"Please configure the SMTP mail settings first..."), category="error")
    return redirect(url_for('admin.admin'))


@admi.route("/admin/logfile")
@login_required
@admin_required
def view_logfile():
    logfiles = {0: logger.get_logfile(config.config_logfile),
                1: logger.get_accesslogfile(config.config_access_logfile)}
    return render_title_template("logviewer.html",
                                 title=_(u"Logfile viewer"),
                                 accesslog_enable=config.config_access_log,
                                 log_enable=bool(config.config_logfile != logger.LOG_TO_STDOUT),
                                 logfiles=logfiles,
                                 page="logfile")


@admi.route("/ajax/log/<int:logtype>")
@login_required
@admin_required
def send_logfile(logtype):
    if logtype == 1:
        logfile = logger.get_accesslogfile(config.config_access_logfile)
        return send_from_directory(os.path.dirname(logfile),
                                   os.path.basename(logfile))
    if logtype == 0:
        logfile = logger.get_logfile(config.config_logfile)
        return send_from_directory(os.path.dirname(logfile),
                                   os.path.basename(logfile))
    else:
        return ""


@admi.route("/admin/logdownload/<int:logtype>")
@login_required
@admin_required
def download_log(logtype):
    if logtype == 0:
        file_name = logger.get_logfile(config.config_logfile)
    elif logtype == 1:
        file_name = logger.get_accesslogfile(config.config_access_logfile)
    else:
        abort(404)
    if logger.is_valid_logfile(file_name):
        return debug_info.assemble_logfiles(file_name)
    abort(404)


@admi.route("/admin/debug")
@login_required
@admin_required
def download_debug():
    return debug_info.send_debug()


@admi.route("/get_update_status", methods=['GET'])
@login_required
@admin_required
def get_update_status():
    if feature_support['updater']:
        log.info(u"Update status requested")
        return updater_thread.get_available_updates(request.method, locale=get_locale())
    else:
        return ''


@admi.route("/get_updater_status", methods=['GET', 'POST'])
@login_required
@admin_required
def get_updater_status():
    status = {}
    if feature_support['updater']:
        if request.method == "POST":
            commit = request.form.to_dict()
            if "start" in commit and commit['start'] == 'True':
                text = {
                    "1": _(u'Requesting update package'),
                    "2": _(u'Downloading update package'),
                    "3": _(u'Unzipping update package'),
                    "4": _(u'Replacing files'),
                    "5": _(u'Database connections are closed'),
                    "6": _(u'Stopping server'),
                    "7": _(u'Update finished, please press okay and reload page'),
                    "8": _(u'Update failed:') + u' ' + _(u'HTTP Error'),
                    "9": _(u'Update failed:') + u' ' + _(u'Connection error'),
                    "10": _(u'Update failed:') + u' ' + _(u'Timeout while establishing connection'),
                    "11": _(u'Update failed:') + u' ' + _(u'General error'),
                    "12": _(u'Update failed:') + u' ' + _(u'Update File Could Not be Saved in Temp Dir')
                }
                status['text'] = text
                updater_thread.status = 0
                updater_thread.resume()
                status['status'] = updater_thread.get_update_status()
        elif request.method == "GET":
            try:
                status['status'] = updater_thread.get_update_status()
                if status['status'] == -1:
                    status['status'] = 7
            except Exception:
                status['status'] = 11
        return json.dumps(status)
    return ''


def ldap_import_create_user(user, user_data):
    user_login_field = extract_dynamic_field_from_filter(user, config.config_ldap_user_object)

    username = user_data[user_login_field][0].decode('utf-8')
    # check for duplicate username
    if ub.session.query(ub.User).filter(func.lower(ub.User.name) == username.lower()).first():
        # if ub.session.query(ub.User).filter(ub.User.name == username).first():
        log.warning("LDAP User  %s Already in Database", user_data)
        return 0, None

    kindlemail = ''
    if 'mail' in user_data:
        useremail = user_data['mail'][0].decode('utf-8')
        if len(user_data['mail']) > 1:
            kindlemail = user_data['mail'][1].decode('utf-8')

    else:
        log.debug('No Mail Field Found in LDAP Response')
        useremail = username + '@email.com'

    try:
        # check for duplicate email
        useremail = check_email(useremail)
    except Exception as ex:
        log.warning("LDAP Email Error: {}, {}".format(user_data, ex))
        return 0, None
    content = ub.User()
    content.name = username
    content.password = ''  # dummy password which will be replaced by ldap one
    content.email = useremail
    content.kindle_mail = kindlemail
    content.role = config.config_default_role
    content.sidebar_view = config.config_default_show
    content.allowed_tags = config.config_allowed_tags
    content.denied_tags = config.config_denied_tags
    content.allowed_column_value = config.config_allowed_column_value
    content.denied_column_value = config.config_denied_column_value
    ub.session.add(content)
    try:
        ub.session.commit()
        return 1, None    # increase no of users
    except Exception as ex:
        log.warning("Failed to create LDAP user: %s - %s", user, ex)
        ub.session.rollback()
        message = _(u'Failed to Create at Least One LDAP User')
        return 0, message


@admi.route('/import_ldap_users')
@login_required
@admin_required
def import_ldap_users():
    showtext = {}
    try:
        new_users = services.ldap.get_group_members(config.config_ldap_group_name)
    except (services.ldap.LDAPException, TypeError, AttributeError, KeyError) as e:
        log.debug_or_exception(e)
        showtext['text'] = _(u'Error: %(ldaperror)s', ldaperror=e)
        return json.dumps(showtext)
    if not new_users:
        log.debug('LDAP empty response')
        showtext['text'] = _(u'Error: No user returned in response of LDAP server')
        return json.dumps(showtext)

    imported = 0
    for username in new_users:
        user = username.decode('utf-8')
        if '=' in user:
            # if member object field is empty take user object as filter
            if config.config_ldap_member_user_object:
                query_filter = config.config_ldap_member_user_object
            else:
                query_filter = config.config_ldap_user_object
            try:
                user_identifier = extract_user_identifier(user, query_filter)
            except Exception as ex:
                log.warning(ex)
                continue
        else:
            user_identifier = user
            query_filter = None
        try:
            user_data = services.ldap.get_object_details(user=user_identifier, query_filter=query_filter)
        except AttributeError as ex:
            log.debug_or_exception(ex)
            continue
        if user_data:
            user_count, message = ldap_import_create_user(user, user_data)
            if message:
                showtext['text'] = message
            else:
                imported += user_count
        else:
            log.warning("LDAP User: %s Not Found", user)
            showtext['text'] = _(u'At Least One LDAP User Not Found in Database')
    if not showtext:
        showtext['text'] = _(u'{} User Successfully Imported'.format(imported))
    return json.dumps(showtext)


def extract_user_data_from_field(user, field):
    match = re.search(field + r"=([\d\s\w-]+)", user, re.IGNORECASE | re.UNICODE)
    if match:
        return match.group(1)
    else:
        raise Exception("Could Not Parse LDAP User: {}".format(user))


def extract_dynamic_field_from_filter(user, filtr):
    match = re.search("([a-zA-Z0-9-]+)=%s", filtr, re.IGNORECASE | re.UNICODE)
    if match:
        return match.group(1)
    else:
        raise Exception("Could Not Parse LDAP Userfield: {}", user)


def extract_user_identifier(user, filtr):
    dynamic_field = extract_dynamic_field_from_filter(user, filtr)
    return extract_user_data_from_field(user, dynamic_field)
