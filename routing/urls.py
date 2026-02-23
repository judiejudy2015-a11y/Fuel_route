from django.urls import path
# from .views import home
from .views import RouteView

urlpatterns = [
    # path('', home),
    path("route/", RouteView.as_view(), name="route"),
]
