
from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("register/", views.register, name="register"),
    path("login/", views.user_login, name="login"),
    path("logout/", views.user_logout, name="logout"),

    path("cart/", views.cart_view, name="cart"),
    path("add-to-cart/<int:product_id>/",views.add_to_cart, name="add_to_cart"),
    path("update-cart/<int:item_id>/",views.update_cart, name="update_cart"),
    path("remove-from-cart/<int:item_id>/",views.remove_from_cart, name="remove_from_cart"),
    path("checkout/", views.checkout, name="checkout"),
    path("verify-payment/", views.verify_payment, name="verify_payment"),
]
