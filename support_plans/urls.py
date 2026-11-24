from django.urls import path

from . import behavior_views, views

app_name = 'support_plans'

urlpatterns = [
    path('', views.PlanListView.as_view(), name='list'),
    path('renewal/', views.PlanRenewalView.as_view(), name='renewal'),
    # 支援計画シート（2 枚で 1 組：支援計画シート・支援手順書 兼 記録用紙）
    path('behavior/', behavior_views.SheetListView.as_view(), name='behavior_list'),
    path('behavior/new/', behavior_views.SheetCreateView.as_view(), name='behavior_create'),
    path('behavior/<int:pk>/', behavior_views.SheetEditView.as_view(), name='behavior_edit'),
    path('behavior/<int:pk>/copy/', behavior_views.SheetCopyView.as_view(), name='behavior_copy'),
    path('behavior/<int:pk>/delete/', behavior_views.SheetDeleteView.as_view(), name='behavior_delete'),
    path('behavior/<int:pk>/print/', behavior_views.SheetPrintView.as_view(), name='behavior_print'),
    path('behavior/<int:pk>/ai/', behavior_views.SheetAiView.as_view(), name='behavior_ai'),
    path('new/', views.PlanCreateView.as_view(), name='create'),
    path('<int:pk>/', views.PlanDetailView.as_view(), name='detail'),
    path('<int:pk>/print/', views.PlanPrintView.as_view(), name='print'),
    path('<int:pk>/pdf/', views.PlanPdfView.as_view(), name='pdf'),
    path('<int:pk>/monitoring-report/', views.MonitoringReportView.as_view(), name='monitoring_report'),
    path('<int:pk>/monitoring-report/pdf/', views.MonitoringReportPdfView.as_view(), name='monitoring_pdf'),
    path('<int:pk>/step/<int:n>/', views.PlanStepView.as_view(), name='step'),
    path('<int:pk>/step/<int:n>/reopen/', views.PlanStepReopenView.as_view(), name='step_reopen'),
    path('<int:pk>/ai/assessment/', views.AssessmentDraftView.as_view(), name='ai_assessment'),
    path('<int:pk>/ai/draft/', views.PlanDraftView.as_view(), name='ai_draft'),
    path('<int:pk>/ai/monitoring/', views.MonitoringDraftView.as_view(), name='ai_monitoring'),
    path('<int:pk>/fill/assessment/', views.AssessmentFillView.as_view(), name='fill_assessment'),
    path('<int:pk>/sheet/', views.PlanSheetView.as_view(), name='sheet'),
    path('<int:pk>/sheet/ai/', views.PlanSheetAiView.as_view(), name='sheet_ai'),
    path('<int:pk>/goals/<int:goal_pk>/evidence/', views.GoalEvidenceView.as_view(), name='goal_evidence'),
    path('<int:pk>/goals/add/', views.GoalSaveView.as_view(), name='goal_add'),
    path('<int:pk>/goals/<int:goal_pk>/edit/', views.GoalSaveView.as_view(), name='goal_edit'),
    path('<int:pk>/goals/<int:goal_pk>/delete/', views.GoalDeleteView.as_view(), name='goal_delete'),
    path('<int:pk>/monitoring/new/', views.MonitoringRecordCreateView.as_view(), name='monitoring_add'),
    path('<int:pk>/monitoring/<int:record_pk>/delete/', views.MonitoringRecordDeleteView.as_view(), name='monitoring_delete'),
    path('<int:pk>/successor/', views.SuccessorCreateView.as_view(), name='successor'),
]
