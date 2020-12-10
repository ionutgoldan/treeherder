from datetime import timedelta

from django.db.models import Q, QuerySet
from django.utils import timezone

from treeherder.changelog.models import Changelog


def get_changes(startdate: str = None, enddate: str = None) -> QuerySet:
    """Grabbing the latest changes done in the past days."""
    since_recently = timezone.now() - timedelta(days=15)
    startdate = startdate or since_recently

    filters = Q(date__gte=startdate)
    if enddate:
        filters = filters & Q(date__lte=enddate)

    return Changelog.objects.filter(filters).order_by("date")
