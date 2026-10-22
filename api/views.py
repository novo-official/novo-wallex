from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import sources


def _mode(request) -> str:
    mode = request.query_params.get("mode", "shadow")
    if mode not in sources.MODES:
        raise ValidationError({"mode": f"one of {sources.MODES}"})
    return mode


@api_view(["GET"])
@permission_classes([AllowAny])  # read-only engine files, served on localhost for the dashboard
def overview(request):
    return Response(sources.overview(settings.BASE_DIR, _mode(request)))


@api_view(["GET"])
@permission_classes([AllowAny])  # read-only engine files, served on localhost for the dashboard
def decisions(request):
    limit = min(int(request.query_params.get("limit", 300)), 5000)
    kinds = {k for k in request.query_params.get("kind", "").split(",") if k} or None
    rows = sources.decisions(settings.BASE_DIR, _mode(request), limit, kinds)
    return Response({"count": len(rows), "results": rows})
