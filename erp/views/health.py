import logging
from django.db import connection,DatabaseError
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:cursor.execute('SELECT 1');cursor.fetchone()
    except DatabaseError:
        logging.getLogger(__name__).error('Database health check failed')
        return JsonResponse({'status':'unavailable','database':'unavailable'},status=503)
    return JsonResponse({'status':'ok','database':'ok'})
