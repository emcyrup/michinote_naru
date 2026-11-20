from django.urls import path

from . import route_views, views

app_name = 'transport'

urlpatterns = [
    path('', views.DayView.as_view(), name='day'),
    path('pdf/', views.DayPdfView.as_view(), name='day_pdf'),
    path('vehicles/', views.VehiclesView.as_view(), name='vehicles'),
    # 便ごとの配車（来所予定で動く事業所）
    path('routes/', route_views.RouteBoardView.as_view(), name='routes'),
    path('routes/new/', route_views.DayRouteEditView.as_view(), name='route_new'),
    path('routes/<int:pk>/', route_views.DayRouteEditView.as_view(), name='route_edit'),
    path('patterns/', route_views.PatternBoardView.as_view(), name='route_patterns'),
    path('patterns/new/', route_views.PatternEditView.as_view(), name='route_pattern_new'),
    path('patterns/<int:pk>/', route_views.PatternEditView.as_view(), name='route_pattern_edit'),
    path('users/<int:pk>/', views.ProfileView.as_view(), name='profile'),
]
