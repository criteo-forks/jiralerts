#!/usr/bin/env python3


import base64
import argparse
import flask
import hashlib
import jinja2
import os
import sys

from gourde import Gourde
import prometheus_client as prometheus
from jira import JIRA


app = flask.Flask(__name__)


# Add our own index.
@app.route('/')
def index():
    return 'jiralert: %s<br/> <a href="/metrics">metrics</a>' % (
        jira.client_info()
    )


gourde = Gourde(app)
app = gourde.app  # This is a flask.Flask() app.
metrics = gourde.metrics

jira = None

LOG_FORMAT = (
    '[%(asctime)s] %(levelname)s %(module)s '
    '[%(filename)s:%(funcName)s:%(lineno)d] (%(thread)d): %(message)s')
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


class Error(Exception):
    """All local errors."""
    pass


def prepare_group_key(gk):
    """Create a unique key for an alert group."""
    return base64.b64encode(gk.encode()).decode()


def prepare_group_label_key(gk):
    """Create a unique key by hashing an alert group."""
    hash_label = hashlib.sha1(gk.encode()).hexdigest()
    return hash_label[0:10]


def prepare_tags(common_labels):
    """Get JIRA tags from alert labels."""
    tags_whitelist = ['severity', 'dc', 'env', 'perimeter', 'team', 'jiralert']
    tags = ['alert', ]
    for k, v in common_labels.items():
        if k in tags_whitelist:
            tags.append('%s:%s' % (k, v))
        if k == 'tags':
            tags.extend([tag.strip() for tag in v.split(',') if tag])
    return tags


JINJA_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT_DIR))
JINJA_ENV.filters['prepareGroupKey'] = prepare_group_key

SUMMARY_TMPL = JINJA_ENV.get_template('templates/summary.tmpl')
DESCRIPTION_TMPL = JINJA_ENV.get_template('templates/description.tmpl')
DESCRIPTION_BOUNDARY = '_-- Alertmanager -- [only edit above]_'

# Order for the search query is important for the query performance. It relies
# on the 'alert_group_key' field in the description that must not be modified.
SEARCH_QUERY = 'project = "{project}" and ' + \
               'issuetype = "{issuetype}" and ' + \
               'labels = "alert" and ' + \
               'status not in ({status}) and ' + \
               '(description ~ "alert_group_key={group_key}" or ' + \
               'labels = "jiralert:{group_label_key}")'

jira_request_time = prometheus.Histogram('jira_request_latency_seconds',
                                         'Latency when querying the JIRA API',
                                         ['action'])
jira_request_time_transitions = jira_request_time.labels(action='transitions')
jira_request_time_close = jira_request_time.labels(action='close')
jira_request_time_update = jira_request_time.labels(action='update')
jira_request_time_create = jira_request_time.labels(action='create')

request_time = prometheus.Histogram('request_latency_seconds',
                                    'Latency of incoming requests',
                                    ['endpoint'])
request_time_generic_issues = request_time.labels(endpoint='/issues')
request_time_qualified_issues = request_time.labels(
    endpoint='/issues/<project>/<issue_type>')


@jira_request_time_transitions.time()
def transitions(issue):
    return jira.transitions(issue)


@jira_request_time_close.time()
def close(issue, tid):
    return jira.transition_issue(issue, tid)


@jira_request_time_update.time()
def update_issue(issue, summary, description, tags):
    custom_desc = issue.fields.description.rsplit(DESCRIPTION_BOUNDARY, 1)[0]

    # Merge expected tags and existing ones
    fields = {"labels": list(set(issue.fields.labels + tags))}

    return issue.update(
        summary=summary,
        fields=fields,
        description="%s\n\n%s\n%s" % (custom_desc.strip(), DESCRIPTION_BOUNDARY, description))


@jira_request_time_create.time()
def create_issue(project, issue_type, summary, description, tags):
    return jira.create_issue({
        'project': {'key': project},
        'summary': summary,
        'description': "%s\n\n%s" % (DESCRIPTION_BOUNDARY, description),
        'issuetype': {'name': issue_type},
        'labels': tags,
    })


@request_time_generic_issues.time()
@app.route('/issues', methods=['POST'])
def parse_issue_params():
    """
    This endpoint accepts a JSON encoded notification according to the version 3 or 4
    of the generic webhook of the Prometheus Alertmanager.
    """
    data = flask.request.get_json()
    if data['version'] not in ["3", "4"]:
        app.logger.error("/issue, unknown message version: %s" % data['version'])
        return "unknown message version %s" % data['version'], 400

    common_labels = data['commonLabels']
    if 'issue_type' not in common_labels or 'project' not in common_labels:
        app.logger.error("/issue, required commonLabels not found: issue_type or project")
        return "Required commonLabels not found: issue_type or project", 400

    issue_type = common_labels['issue_type']
    project = common_labels['project']
    return file_issue(project=project, issue_type=issue_type)


def update_or_resolve_issue(project, issue_type, issue, resolved, summary, description, tags):
    """Update and maybe resolve an issue."""
    app.logger.debug("issue (%s, %s), jira issue found: %s" % (
        project, issue_type, issue.key))

    # Try different possible transitions for resolved incidents
    # in order of preference. Different ones may work for different boards.
    if resolved:
        valid_trans = [
            t for t in transitions(issue) if t['name'].lower() in resolve_transitions]
        if valid_trans:
            close(issue, valid_trans[0]['id'])
        else:
            app.logger.warning(
                "Unable to find transition to close %s" % issue)

    # Update the base information regardless of the transition.
    update_issue(issue, summary, description, tags)
    app.logger.debug("issue (%s, %s), %s updated" % (project, issue_type, issue.key))


@request_time_qualified_issues.time()
@app.route('/issues/<project>/<issue_type>', methods=['POST'])
def file_issue(project, issue_type):
    """
    This endpoint accepts a JSON encoded notification according to the version 3 or 4
    of the generic webhook of the Prometheus Alertmanager.
    """
    app.logger.info("issue: %s %s" % (project, issue_type))

    data = flask.request.get_json()
    if data['version'] not in ["3", "4"]:
        app.logger.error("issue (%s, %s), unknown message version: %s" % (
            project, issue_type, data['version']))
        return "unknown message version %s" % data['version'], 400

    resolved = data['status'] == "resolved"
    tags = prepare_tags(data['commonLabels'])
    tags.append('jiralert:%s' % prepare_group_label_key(data['groupKey']))

    description = DESCRIPTION_TMPL.render(data)
    summary = SUMMARY_TMPL.render(data)

    # If there's already a ticket for the incident, update it and close if necessary.
    query = SEARCH_QUERY.format(
        project=project,
        issuetype=issue_type,
        status=','.join(resolved_status),
        group_key=prepare_group_key(data['groupKey']),
        group_label_key=prepare_group_label_key(data['groupKey'])
    )
    app.logger.debug(query)
    result = jira.search_issues(query) or []
    # sort issue by key to have them in order of creation.
    sorted(result, key=lambda i: i.key)

    for issue in result:
        update_or_resolve_issue(project, issue_type, issue, resolved, summary, description, tags)
    if not result:
        # Do not create an issue for resolved incidents that were never filed.
        if not resolved:
            issue = create_issue(project, issue_type, summary, description, tags)
            app.logger.debug("issue (%s, %s), new issue created (%s)" % (
                project, issue_type, issue.key))

    return "", 200


def setup_app(server, res_transitions, res_status):
    """Setup the app itself."""
    # TODO: get rid of globals. Maybe store that inside app. itself.
    global jira
    global resolve_transitions, resolved_status

    resolve_transitions = res_transitions.split(',')
    resolved_status = res_status.split(',')

    username = os.environ.get('JIRA_USERNAME')
    password = os.environ.get('JIRA_PASSWORD')
    if not username or not password:
        print("JIRA_USERNAME or JIRA_PASSWORD not set")
        sys.exit(2)

    app.logger.info("Connecting to JIRA.""")
    jira = JIRA(basic_auth=(username, password), server=server, logging=True)
    return app


def setup(args):
    """Setup everything."""
    setup_app(args.server, args.res_transitions, args.res_status)


def main():
    # Setup a custom parser.
    parser = argparse.ArgumentParser(description='jiralert')
    parser = Gourde.get_argparser(parser)
    # Backward compatibility.
    parser.add_argument(
        '--loglevel', default='INFO', help='Log Level, empty string to disable.'
    )
    parser.add_argument(
        '--res_transitions', default="resolve issue,close issue",
        help='Comma separated list of known transitions used to resolve alerts'
    )
    parser.add_argument(
        '--res_status', default="resolved,closed,done,complete",
        help='Comma separated list of known resolved status'
    )
    parser.add_argument('server')
    args = parser.parse_args()
    args.log_level = args.loglevel

    if args.twisted:
        from twisted.internet import reactor
        reactor.callInThread(setup, args)
    else:
        setup(args)

    # Setup gourde with the args.
    gourde.setup(args)
    gourde.is_healthy = lambda: jira is not None and bool(jira.client_info())
    gourde.is_ready = lambda: jira is not None
    gourde.run()


if __name__ == "__main__":
    main()
