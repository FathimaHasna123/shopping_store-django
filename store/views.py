from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm

from .models import Product, Cart, CartItem, Order
from .forms import RegisterForm

import os
import razorpay

from django.conf import settings
from django.db import transaction
from django.views.decorators.http import require_POST


# Create your views here.



def home(request):
    products = Product.objects.all()
    return render(request, "store/home.html", {
        "products": products
    })


def register(request):
    if request.method == "POST":
        form = RegisterForm(request.POST)

        if form.is_valid():
            user = form.save()
            Cart.objects.create(user=user)
            login(request, user)
            return redirect("home")
    else:
        form = RegisterForm()

    return render(request, "store/register.html", {
        "form": form
    })


def user_login(request):
    if request.method == "POST":
        form = AuthenticationForm(
            request, data=request.POST
        )

        if form.is_valid():
            login(request, form.get_user())
            return redirect("home")
    else:
        form = AuthenticationForm()

    return render(request, "store/login.html", {
        "form": form
    })


def user_logout(request):
    if request.method == "POST":
        logout(request)

    return redirect("home")


@login_required
def add_to_cart(request, product_id):
    if request.method != "POST":
        return redirect("home")

    product = get_object_or_404(Product, id=product_id)

    # Get the user's cart or create one if missing.
    cart, created = Cart.objects.get_or_create(
        user=request.user
    )

    item, created = CartItem.objects.get_or_create(
        cart=cart, product=product,
        defaults={"quantity": 1}
    )

    if not created:
        item.quantity += 1

    if item.quantity > product.stock:
        messages.error(request, "Not enough stock available.")
    else:
        item.save()
        messages.success(request, "Product added to cart!")

    return redirect("cart")


@login_required
def cart_view(request):
    cart, created = Cart.objects.get_or_create(
        user=request.user
    )
    items = cart.items.select_related("product").all()

    total = sum(
        (item.subtotal() for item in items),
        Decimal("0.00")
    )

    return render(request, "store/cart.html", {
        "items": items,
        "total": total
    })


@login_required
def update_cart(request, item_id):
    if request.method == "POST":
        item = get_object_or_404(
            CartItem, id=item_id, cart__user=request.user
        )
        try:
            quantity = int(request.POST.get("quantity", 1))

            if 1 <= quantity <= item.product.stock:
                item.quantity = quantity
                item.save()
            else:
                messages.error(request, "Invalid quantity.")
        except (ValueError, TypeError):
            messages.error(request, "Enter a valid quantity.")

    return redirect("cart")


@login_required
def remove_from_cart(request, item_id):
    if request.method == "POST":
        item = get_object_or_404(
            CartItem, id=item_id, cart__user=request.user
        )
        item.delete()

    return redirect("cart")

client = razorpay.Client(
    auth=(
        os.environ.get("RAZORPAY_KEY_ID", ""),
        os.environ.get("RAZORPAY_KEY_SECRET", ""),
    )
)


@login_required
def checkout(request):
    cart, created = Cart.objects.get_or_create(
        user=request.user
    )
    items = list(
        cart.items.select_related("product").all()
    )

    if not items:
        messages.error(request, "Your cart is empty.")
        return redirect("cart")

    # Check stock before creating a payment order.
    for item in items:
        if item.quantity > item.product.stock:
            messages.error(
                request,
                f"Insufficient stock for {item.product.name}."
            )
            return redirect("cart")

    total = sum(
        (item.subtotal() for item in items),
        Decimal("0.00")
    )

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")

    if not key_id or not key_secret:
        messages.error(request, "Payment keys are not configured.")
        return redirect("cart")

    # Create a local order first, with a pending status.
    order = Order.objects.create(
        user=request.user,
        total_amount=total
    )

    try:
        # Razorpay expects the amount in paise.
        razorpay_order = client.order.create({
            "amount": int(total * 100),
            "currency": "INR",
            "receipt": f"order_{order.id}",
        })

        order.razorpay_order_id = razorpay_order["id"]
        order.save(update_fields=["razorpay_order_id"])

    except Exception:
        order.status = "failed"
        order.save(update_fields=["status"])
        messages.error(
            request,
            "Could not start payment. Please try again."
        )
        return redirect("cart")

    return render(request, "store/checkout.html", {
        "order": order,
        "total": total,
        "key_id": key_id,
        "razorpay_order_id": razorpay_order["id"],
    })


@login_required
@require_POST
def verify_payment(request):
    payment_id = request.POST.get("razorpay_payment_id", "")
    razorpay_order_id = request.POST.get(
        "razorpay_order_id", ""
    )
    signature = request.POST.get("razorpay_signature", "")

    # Only accept an order belonging to the logged-in user.
    order = get_object_or_404(
        Order,
        user=request.user,
        razorpay_order_id=razorpay_order_id,
        status="pending",
    )

    try:
        # Verify the signature using Razorpay's SDK.
        client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })

        # Fetch the payment from Razorpay and confirm its
        # order, amount, currency and captured status.
        payment = client.payment.fetch(payment_id)

        if (
            payment.get("order_id") != razorpay_order_id
            or payment.get("amount") != int(order.total_amount * 100)
            or payment.get("currency") != "INR"
            or payment.get("status") != "captured"
        ):
            messages.error(
                request,
                "Payment has not been confirmed as captured."
            )
            return redirect("cart")

        # Check stock again, then update stock and payment
        # status together in a database transaction.
        with transaction.atomic():
            locked_order = Order.objects.select_for_update().get(
                pk=order.pk, status="pending"
            )
            cart = Cart.objects.get(user=request.user)
            items = list(
                cart.items.select_related("product").all()
            )

            if not items:
                messages.error(request, "Your cart is empty.")
                return redirect("cart")

            for item in items:
                product = type(item.product).objects.select_for_update().get(
                    pk=item.product_id
                )
                if item.quantity > product.stock:
                    messages.error(
                        request,
                        "Payment captured, but stock needs manual review."
                    )
                    return redirect("cart")

            for item in items:
                product = type(item.product).objects.select_for_update().get(
                    pk=item.product_id
                )
                product.stock -= item.quantity
                product.save(update_fields=["stock"])

            locked_order.status = "paid"
            locked_order.razorpay_payment_id = payment_id
            locked_order.save(
                update_fields=["status", "razorpay_payment_id"]
            )

            cart.items.all().delete()

    except Exception:
        messages.error(
            request,
            "Payment could not be verified. Please contact support "
            "if your payment was deducted."
        )
        return redirect("cart")

    return render(request, "store/payment_success.html", {
        "order": order
    })
