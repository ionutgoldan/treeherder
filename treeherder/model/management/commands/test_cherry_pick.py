import logging

from django.core.management.base import BaseCommand
from treeherder.perf.alerts import cherry_picked_alerts

logging.basicConfig(format='%(levelname)s:%(message)s')

class Command(BaseCommand):
    help = """Cycle data that exceeds the time constraint limit"""

    def add_arguments(self, parser):
        parser.add_argument(
            '--summary-id',
            action='store',
            help='Write debug messages to stdout'
        )

    def handle(self, *args, **options):
        from pprint import pprint
        pprint(cherry_picked_alerts(options['summary_id']))
