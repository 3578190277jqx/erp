from .models import Membership


class PermissionContextMiddleware:
    """Permission inputs live for one request only, never across requests."""
    def __init__(self,get_response):
        self.get_response=get_response

    def __call__(self,request):
        if request.user.is_authenticated:
            request.user._erp_memberships=list(Membership.objects.filter(user=request.user).select_related('organization'))
        return self.get_response(request)
