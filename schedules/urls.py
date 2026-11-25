from django.urls import path
from . import views

app_name = 'schedules'

urlpatterns = [
    # 実績記録票（日ごと・子どもごとの 1 か月ぶん）
    path('jisseki/', views.JissekiDayView.as_view(), name='jisseki'),
    path('jisseki/<int:beneficiary_pk>/', views.JissekiMonthView.as_view(), name='jisseki_child'),
    path('jisseki/<int:beneficiary_pk>/<int:year>/<int:month>/', views.JissekiMonthView.as_view(), name='jisseki_month'),
    path('jisseki/print/<int:year>/<int:month>/', views.JissekiPrintView.as_view(), name='jisseki_print_all'),
    path('jisseki/<int:beneficiary_pk>/<int:year>/<int:month>/print/', views.JissekiPrintView.as_view(), name='jisseki_print'),

    # 月表示カレンダー（当月 / 指定月）
    path('',
         views.CalendarView.as_view(), name='calendar'),
    path('<int:year>/<int:month>/',
         views.CalendarView.as_view(), name='calendar_month'),

    # 日別出欠入力
    path('<int:year>/<int:month>/<int:day>/',
         views.DailyView.as_view(), name='daily'),
    path('<int:year>/<int:month>/<int:day>/save/',
         views.DailySaveView.as_view(), name='daily_save'),

    # 予定を一括作成
    path('<int:year>/<int:month>/bulk-create/',
         views.BulkCreateView.as_view(), name='bulk_create'),
]
