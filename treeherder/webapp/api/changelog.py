from rest_framework import viewsets
from rest_framework.response import Response

from treeherder.changelog.changes import get_changes

from .serializers import ChangelogSerializer


class ChangelogViewSet(viewsets.ViewSet):
    """
    This viewset is responsible for the changelog endpoint.
    """

    def list(self, request):
        """
        GET method implementation for list view
        """
        startdate = request.query_params.get('startdate')
        enddate = request.query_params.get('enddate')

        serializer = ChangelogSerializer(get_changes(startdate, enddate), many=True)
        return Response(serializer.data)
