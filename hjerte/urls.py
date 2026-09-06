from django.contrib import admin
from django.contrib.auth.views import LogoutView, PasswordChangeView, PasswordChangeDoneView
from django.urls import path
from study import views

admin.site.site_header='Hjerte administration'
admin.site.site_title='Hjerte'
admin.site.login=views.login_view
urlpatterns=[
    path('',views.dashboard,name='dashboard'),path('login/',views.login_view,name='login'),
    path('logout/',LogoutView.as_view(),name='logout'),
    path('password/',PasswordChangeView.as_view(template_name='registration/password.html'),name='password_change'),
    path('password/done/',PasswordChangeDoneView.as_view(template_name='registration/password_done.html'),name='password_change_done'),
    path('practice/',views.practice,name='practice'),path('practice/daily/',views.daily,name='daily'),
    path('exam/',views.exam,name='exam'),path('progress/',views.progress_view,name='progress'),
    path('library/',views.library,name='library'),path('library/<int:source_id>/file/',views.source_file,name='source_file'),
    path('studio/',views.studio,name='studio'),path('admin/',admin.site.urls),
    path('studio/coverage/',views.coverage_view,name='coverage'),
    path('studio/sources/<int:source_id>/',views.source_setup,name='source_setup'),
    path('sessions/<uuid:session_id>/',views.session_view,name='session'),
    path('sessions/<uuid:session_id>/answer/<int:position>/',views.answer,name='answer'),
    path('sessions/<uuid:session_id>/flag/<int:position>/',views.flag,name='flag'),
    path('sessions/<uuid:session_id>/finish/',views.finish,name='finish'),
    path('sessions/<uuid:session_id>/results/',views.results,name='results'),
    path('health/',views.health,name='health'),path('manifest.webmanifest',views.manifest),
    path('sw.js',views.service_worker),path('offline/',views.offline),
]
