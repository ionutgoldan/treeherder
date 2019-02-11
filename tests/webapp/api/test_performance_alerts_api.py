import copy
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils.timezone import now as django_now
from first import first

from treeherder.perf.models import (PerformanceAlert,
                                    PerformanceAlertSummary,
                                    PerformanceFramework)


def test_alerts_get(client, test_repository, test_perf_alert):
    resp = client.get(reverse('performance-alerts-list'))
    assert resp.status_code == 200

    # should just have the one alert
    assert resp.json()['next'] is None
    assert resp.json()['previous'] is None
    assert len(resp.json()['results']) == 1
    assert set(resp.json()['results'][0].keys()) == set([
        'amount_pct',
        'amount_abs',
        'id',
        'is_regression',
        'starred',
        'manually_created',
        'new_value',
        'prev_value',
        'related_summary_id',
        'series_signature',
        'summary_id',
        'status',
        't_value',
        'classifier',
        'classifier_email'
    ])
    assert resp.json()['results'][0]['related_summary_id'] is None


def test_alerts_put(client, push_stored, test_repository,
                    test_perf_alert, test_perf_alert_summary_2, test_user,
                    test_sheriff):
    resp = client.get(reverse('performance-alerts-list'))
    assert resp.status_code == 200
    assert resp.json()['results'][0]['related_summary_id'] is None

    # verify that we fail if not authenticated
    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'related_summary_id': 2,
        'status': PerformanceAlert.DOWNSTREAM
    })
    assert resp.status_code == 403
    assert PerformanceAlert.objects.get(id=1).related_summary_id is None

    # verify that we fail if authenticated, but not staff
    client.force_authenticate(user=test_user)
    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'related_summary_id': 2,
        'status': PerformanceAlert.DOWNSTREAM
    })
    assert resp.status_code == 403
    assert PerformanceAlert.objects.get(id=1).related_summary_id is None

    # verify that we succeed if authenticated + staff
    client.force_authenticate(user=test_sheriff)
    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'related_summary_id': 2,
        'status': PerformanceAlert.DOWNSTREAM
    })
    assert resp.status_code == 200
    assert PerformanceAlert.objects.get(id=1).related_summary_id == 2
    assert PerformanceAlert.objects.get(id=1).classifier == test_sheriff

    # verify that we can unset it too
    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'related_summary_id': None,
        'status': PerformanceAlert.UNTRIAGED
    })
    assert resp.status_code == 200
    assert PerformanceAlert.objects.get(id=1).related_summary_id is None


def test_reassign_different_repository(client,
                                       push_stored,
                                       test_repository, test_repository_2,
                                       test_perf_alert,
                                       test_perf_alert_summary_2,
                                       test_sheriff):
    # verify that we can't reassign to another performance alert summary
    # with a different repository unless the new status is downstream
    test_perf_alert_summary_2.repository = test_repository_2
    test_perf_alert_summary_2.save()

    client.force_authenticate(user=test_sheriff)

    # reassign to summary with different repository, should fail
    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'related_summary_id': test_perf_alert_summary_2.id,
        'status': PerformanceAlert.REASSIGNED
    })
    assert resp.status_code == 400
    test_perf_alert.refresh_from_db()
    assert test_perf_alert.related_summary_id is None
    assert test_perf_alert.status == PerformanceAlert.UNTRIAGED

    # mark downstream of summary with different repository,
    # should succeed
    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'related_summary_id': test_perf_alert_summary_2.id,
        'status': PerformanceAlert.DOWNSTREAM
    })
    assert resp.status_code == 200
    test_perf_alert.refresh_from_db()
    assert test_perf_alert.related_summary_id == test_perf_alert_summary_2.id
    assert test_perf_alert.classifier == test_sheriff


def test_reassign_different_framework(client,
                                      push_stored,
                                      test_repository, test_repository_2,
                                      test_perf_alert,
                                      test_perf_alert_summary_2,
                                      test_sheriff):
    # try to assign to an alert with a different framework,
    # should fail
    framework_2 = PerformanceFramework.objects.create(
        name='test_talos_2', enabled=True)
    test_perf_alert_summary_2.framework = framework_2
    test_perf_alert_summary_2.save()

    client.force_authenticate(user=test_sheriff)

    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'related_summary_id': test_perf_alert_summary_2.id,
        'status': PerformanceAlert.REASSIGNED
    })
    assert resp.status_code == 400
    test_perf_alert.refresh_from_db()
    assert test_perf_alert.related_summary_id is None
    assert test_perf_alert.status == PerformanceAlert.UNTRIAGED


@pytest.fixture
def alert_create_post_blob(test_perf_alert_summary, test_perf_signature):
    # this blob should be sufficient to create a new alert (assuming
    # the user of this API is authorized to do so!)
    return {
        'summary_id': test_perf_alert_summary.id,
        'signature_id': test_perf_signature.id
    }


def test_alerts_post(client, alert_create_post_blob,
                     test_user, test_sheriff, generate_enough_perf_datum):

    # verify that we fail if not authenticated
    resp = client.post(reverse('performance-alerts-list'),
                       alert_create_post_blob)
    assert resp.status_code == 403

    # verify that we fail if authenticated, but not staff
    client.force_authenticate(user=test_user)
    resp = client.post(reverse('performance-alerts-list'),
                       alert_create_post_blob)
    assert resp.status_code == 403
    assert PerformanceAlert.objects.count() == 0

    # verify that we succeed if staff + authenticated
    client.force_authenticate(user=test_sheriff)
    resp = client.post(reverse('performance-alerts-list'),
                       alert_create_post_blob)
    assert resp.status_code == 200
    assert PerformanceAlert.objects.count() == 1

    alert = PerformanceAlert.objects.first()
    assert alert.status == PerformanceAlert.UNTRIAGED
    assert alert.manually_created
    assert alert.amount_pct == 100
    assert alert.amount_abs == 1
    assert alert.prev_value == 1
    assert alert.new_value == 2
    assert alert.is_regression
    assert alert.summary.id == 1


def test_alerts_post_insufficient_data(client,
                                       test_repository,
                                       test_perf_alert_summary,
                                       test_perf_signature, test_sheriff,
                                       alert_create_post_blob):
    client.force_authenticate(user=test_sheriff)
    # we should not succeed if insufficient data is passed through
    for removed_key in ['summary_id', 'signature_id']:
        new_post_blob = copy.copy(alert_create_post_blob)
        del new_post_blob[removed_key]

        resp = client.post(reverse('performance-alerts-list'),
                           new_post_blob)
        assert resp.status_code == 400
        assert PerformanceAlert.objects.count() == 0


@pytest.mark.parametrize("perf_datum_id, towards_push_ids",
                         [(3, {'prev_push_id': 1, 'push_id': 2}),
                          (2, {'prev_push_id': 2, 'push_id': 3})])
def test_nudge_alert_to_changeset_without_alert_summary(client,
                                                        test_sheriff,
                                                        test_perf_alert,
                                                        test_perf_data,
                                                        perf_datum_id,
                                                        towards_push_ids):
    client.force_authenticate(user=test_sheriff)
    link_alert_summary_in_perf_data(test_perf_data, test_perf_alert,
                                    perf_datum_id)

    old_alert_summary_id = test_perf_alert.summary.id

    resp = client.put(reverse('performance-alerts-list') + '1/', towards_push_ids)

    assert resp.status_code == 200

    test_perf_alert.refresh_from_db()
    new_alert_summary = test_perf_alert.summary

    assert new_alert_summary.id != old_alert_summary_id
    assert 'alert_summary_id' in resp.json()
    assert resp.json()['alert_summary_id'] == new_alert_summary.id

    # new summary has correct push ids
    assert new_alert_summary.prev_push_id == towards_push_ids["prev_push_id"]
    assert new_alert_summary.push_id == towards_push_ids["push_id"]

    # old alert summary gets deleted
    assert not PerformanceAlertSummary.objects.filter(pk=old_alert_summary_id).exists()


@pytest.mark.parametrize("perf_datum_ids, alert_id_to_move, towards_push_ids",
                         [((2, 3), 2, {'push_id': 2, 'prev_push_id': 1}),
                          (None, 1, {'push_id': 3, 'prev_push_id': 2})])
def test_nudge_alert_to_changeset_with_an_alert_summary(client,
                                                        test_sheriff,
                                                        test_perf_alert,
                                                        test_perf_alert_2,
                                                        test_perf_alert_summary,
                                                        test_perf_alert_summary_2,
                                                        test_perf_data,
                                                        perf_datum_ids,
                                                        alert_id_to_move,
                                                        towards_push_ids):
    """
    push_ids: 1 [2 summary_2+alert] -nudge-> [3 summary+alert_2] 4
                                    <-nudge-
    """
    client.force_authenticate(user=test_sheriff)

    alert_to_move, starting_summary = test_perf_alert, test_perf_alert_summary_2
    if perf_datum_ids:
        link_alert_summary_in_perf_data(test_perf_data, test_perf_alert,
                                        perf_datum_ids[0])
        link_alert_summary_in_perf_data(test_perf_data, test_perf_alert_2,
                                        perf_datum_ids[1])
        associate_perf_data_to_alert(test_perf_data, test_perf_alert_2)
        alert_to_move, starting_summary = test_perf_alert_2, test_perf_alert_summary
    old_alert_summary_id = alert_to_move.summary.id

    resp = client.put(reverse('performance-alerts-list') + str(alert_id_to_move) + '/', towards_push_ids)

    assert resp.status_code == 200

    test_perf_alert.refresh_from_db()
    test_perf_alert_2.refresh_from_db()
    starting_summary.refresh_from_db()

    assert alert_to_move.summary.id != old_alert_summary_id
    assert 'alert_summary_id' in resp.json()
    assert resp.json()['alert_summary_id'] == alert_to_move.summary.id

    # old alert summary gets deleted
    assert not PerformanceAlertSummary.objects.filter(pk=old_alert_summary_id).exists()

    # prev alert_summary gets properly updated
    assert alert_to_move.summary.id == starting_summary.id
    assert alert_to_move.summary.alerts.count() == 2


def test_nudge_left_alert_from_alert_summary_with_more_alerts(client,
                                                              test_sheriff,
                                                              test_perf_alert,
                                                              test_perf_alert_2,
                                                              test_perf_alert_summary,
                                                              test_perf_alert_summary_2,
                                                              test_perf_data):
    associate_perf_data_to_alert(test_perf_data, test_perf_alert_2)

    client.force_authenticate(user=test_sheriff)

    old_alert_summary_id = test_perf_alert_2.summary.id
    test_perf_alert.summary = test_perf_alert_summary_2
    test_perf_alert.save()

    resp = client.put(reverse('performance-alerts-list') + '2/', {
        'push_id': 2,
        'prev_push_id': 1
    })

    assert resp.status_code == 200

    test_perf_alert.refresh_from_db()
    test_perf_alert_2.refresh_from_db()
    test_perf_alert_summary_2.refresh_from_db()

    assert test_perf_alert_2.summary.id != old_alert_summary_id
    assert 'alert_summary_id' in resp.json()
    assert resp.json()['alert_summary_id'] == test_perf_alert_2.summary.id

    # old alert summary still there
    old_alert_summary = PerformanceAlertSummary.objects.filter(pk=old_alert_summary_id).first()
    assert old_alert_summary is not None
    # with other alert
    assert test_perf_alert in old_alert_summary.alerts.all()

    # prev alert_summary gets properly updated
    assert test_perf_alert_summary_2.alerts.count() == 1


def test_nudge_right_alert_from_alert_summary_with_more_alerts(client,
                                                               test_sheriff,
                                                               test_perf_alert,
                                                               test_perf_alert_2,
                                                               test_perf_alert_summary,
                                                               test_perf_alert_summary_2,
                                                               test_perf_data):
    """
    | push 2          |          | push 3          |
    | --------------- |          | --------------- |
    | | summary     | |          | | summary_2   | |
    | | prev_push=1 | |          | | prev_push=2 | |
    | | ----------- | |          | | ----------- | |
    | | alert       |-|--nudge---|>|_____________| |
    | |_alert_2_____| |          |                 |
    |_________________|          |_________________|
    """

    client.force_authenticate(user=test_sheriff)

    old_alert_summary_id = test_perf_alert.summary.id
    test_perf_alert_2.summary = test_perf_alert_summary
    test_perf_alert_2.save()

    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'push_id': 3,
        'prev_push_id': 2
    })

    assert resp.status_code == 200

    test_perf_alert.refresh_from_db()
    test_perf_alert_2.refresh_from_db()
    test_perf_alert_summary.refresh_from_db()
    test_perf_alert_summary_2.refresh_from_db()

    assert test_perf_alert.summary.id != old_alert_summary_id
    assert 'alert_summary_id' in resp.json()
    assert resp.json()['alert_summary_id'] == test_perf_alert.summary.id

    # old alert summary still there
    assert PerformanceAlertSummary.objects.filter(pk=old_alert_summary_id).count() == 1
    # with other alert
    assert test_perf_alert_2 in PerformanceAlert.objects.filter(summary_id=old_alert_summary_id).all()

    # prev alert_summary gets properly updated
    assert test_perf_alert_summary.alerts.count() == 1


def test_nudge_raises_exception_when_no_perf_data(client,
                                                  test_sheriff,
                                                  test_perf_alert,
                                                  test_perf_alert_summary):
    client.force_authenticate(user=test_sheriff)
    initial_summary_count = PerformanceAlertSummary.objects.all().count()
    initial_alert_count = PerformanceAlert.objects.all().count()

    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'push_id': 3,
        'prev_push_id': 2
    })

    assert resp.status_code == 400
    assert PerformanceAlertSummary.objects.all().count() == initial_summary_count
    assert PerformanceAlert.objects.all().count() == initial_alert_count


def test_nudge_recalculates_alert_properties(client,
                                             test_sheriff,
                                             test_perf_alert,
                                             test_perf_alert_summary,
                                             test_perf_data):

    def _get_alert_properties(test_perf_alert):
        prop_names = ['amount_pct', 'amount_abs', 'prev_value', 'new_value', 't_value']
        return [getattr(test_perf_alert, prop_name) for prop_name in prop_names]

    client.force_authenticate(user=test_sheriff)

    # let's update the performance data
    # so that recalculation produces new results
    for index, perf_datum in enumerate(test_perf_data):
        perf_datum.value = index * 10
        perf_datum.save()

    resp = client.put(reverse('performance-alerts-list') + '1/', {
        'push_id': 3,
        'prev_push_id': 2
    })
    assert resp.status_code == 200
    test_perf_alert.refresh_from_db()

    new_alert_properties = _get_alert_properties(test_perf_alert)
    assert new_alert_properties == [400.0, 20.0, 5.0, 25.0, 20.0]


def test_timestamps_on_alert_and_summaries_inside_code(test_perf_alert_summary,
                                                       test_perf_signature,
                                                       test_perf_signature_2):
    new_alert = PerformanceAlert.objects.create(summary=test_perf_alert_summary,
                                                series_signature=test_perf_signature,
                                                is_regression=True,
                                                amount_pct=10,
                                                amount_abs=10,
                                                prev_value=10,
                                                new_value=11,
                                                t_value=10)
    assert new_alert.created <= new_alert.last_updated
    assert new_alert.first_triaged is None

    # update increases last_updated,
    # but created and first_triaged remain the same
    previous_create = new_alert.created
    previous_update = new_alert.last_updated
    new_alert.starred = True
    new_alert.save()

    assert previous_create == new_alert.created
    assert previous_update < new_alert.last_updated
    assert new_alert.first_triaged is None  # non-human interaction doesn't update this field

    # parent summary aggregates first_triaged from child alerts
    parent_summary = new_alert.summary
    assert parent_summary.first_triaged is None

    sibling_alert = PerformanceAlert.objects.create(summary=test_perf_alert_summary,
                                                    series_signature=test_perf_signature_2,
                                                    is_regression=False,
                                                    amount_pct=20,
                                                    amount_abs=20,
                                                    prev_value=20,
                                                    new_value=21,
                                                    t_value=20)
    sibling_alert.first_triaged = oldest_alert = django_now()
    new_alert.first_triaged = django_now()
    new_alert.save()
    sibling_alert.save()

    assert parent_summary.first_triaged == oldest_alert

    # parent summary's first_triaged remains the oldest
    parent_summary.first_triaged = oldest_interaction = django_now() - timedelta(days=10)

    new_alert.first_triaged = django_now()
    new_alert.save()  # to trigger aggregation on parent

    assert parent_summary.first_triaged == oldest_interaction


def test_timestamps_on_manual_created_alert_via_their_endpoints(client, alert_create_post_blob, test_sheriff,
                                                                generate_enough_perf_datum):
    # created <= last_updated, created <= first_triaged
    # BUT manually_created is True
    client.force_authenticate(user=test_sheriff)
    resp = client.post(reverse('performance-alerts-list'),
                       alert_create_post_blob)
    assert resp.status_code == 200

    manual_alert_id = resp.json()['alert_id']
    manual_alert = PerformanceAlert.objects.get(pk=manual_alert_id)
    assert manual_alert.manually_created is True

    assert manual_alert.created <= manual_alert.last_updated
    assert manual_alert.first_triaged is not None
    assert manual_alert.created <= manual_alert.first_triaged


def test_timestamps_on_alert_via_endpoints(client, test_sheriff, test_perf_alert):
    # updating autogenerated alert:
    # created doesn't change, last_updated & first_triaged update
    old_created = test_perf_alert.created
    old_last_updated = test_perf_alert.last_updated

    client.force_authenticate(user=test_sheriff)
    resp = client.put(reverse('performance-alerts-list') + '1/',
                      {'starred': True})
    assert resp.status_code == 200
    test_perf_alert.refresh_from_db()

    assert test_perf_alert.created == old_created
    assert test_perf_alert.first_triaged is not None
    assert test_perf_alert.created < test_perf_alert.first_triaged
    assert test_perf_alert.last_updated > old_last_updated

    old_first_triaged = test_perf_alert.first_triaged
    old_last_updated = test_perf_alert.last_updated

    # updating alert multiple times:
    # keeps first_triaged the same
    client.force_authenticate(user=test_sheriff)
    resp = client.put(reverse('performance-alerts-list') + '1/',
                      {'status': PerformanceAlert.CONFIRMING})
    assert resp.status_code == 200
    test_perf_alert.refresh_from_db()

    assert test_perf_alert.first_triaged == old_first_triaged
    assert test_perf_alert.last_updated > old_last_updated

    # does this cover the downstream/reassgin use case?
    # do timestamps update on both alert summaries?


def test_timestamps_on_alert_and_summaries_via_nudging():
    # nudging existing autogenerated alert: created remains the same, last_updated updaees, first_triaged updates
    # nudging it again: first_triaged remains the same

    # for each alert, check the alert summary also
    pass


# utils
def link_alert_summary_in_perf_data(test_perf_data, test_perf_alert,
                                    perf_datum_id):
    assert perf_datum_id > 0

    perf_datum = first(test_perf_data, key=lambda tpd: tpd.id == perf_datum_id)
    prev_perf_datum = first(test_perf_data, key=lambda tpd: tpd.id == perf_datum_id-1)

    # adjust relations
    alert_summary = test_perf_alert.summary
    alert_summary.repository = perf_datum.repository
    alert_summary.push = perf_datum.push
    alert_summary.prev_push = prev_perf_datum.push
    alert_summary.save()


def associate_perf_data_to_alert(test_perf_data, test_perf_alert):
    series_signature = test_perf_alert.series_signature

    for perf_datum in test_perf_data:
        perf_datum.signature = series_signature
        perf_datum.save()


def dump_vars(alert_summaries, perf_data, alerts=None):
    from pprint import pprint

    def dump_alert(alert):
        pprint('Alert(id={0.id}, summary_id={0.summary_id}, push_id={0.summary.push_id}, prev_push_id={0.summary.prev_push_id})'.format(alert))
    for summary in alert_summaries:
        pprint('AlertSummary(id={0.id}, push_id={0.push_id}, prev_push_id={0.prev_push_id}) has following alerts: '.format(summary))
        for alert in summary.alerts.all():
            dump_alert(alert)
    if alerts is not None:
        for alert in alerts:
            dump_alert(alert)
    for perf_datum in perf_data:
        pprint('PerfData(id={0.push_id}, push_timestamp={0.push_timestamp})'.format(perf_datum))
