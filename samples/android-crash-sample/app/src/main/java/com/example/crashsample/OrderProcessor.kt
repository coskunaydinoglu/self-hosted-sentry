package com.example.crashsample

data class Cart(val items: Int, val coupon: String?)

/** Business-looking code that R8 renames to a.b.c, so mapping.txt matters. */
class OrderProcessor {

    fun checkout(cart: Cart): Double {
        val subtotal = priceFor(cart.items)
        val discount = discountFor(cart.coupon)
        return applyDiscount(subtotal, discount)
    }

    fun receiptFor(cart: Cart?): String {
        // Deliberate NPE: cart is null.
        return "Receipt for ${cart!!.items} items"
    }

    private fun priceFor(items: Int): Double {
        if (items <= 0) throw IllegalArgumentException("Cart is empty")
        return items * 19.99
    }

    private fun discountFor(coupon: String?): Double = when (coupon) {
        null -> 0.0
        "WELCOME10" -> 0.10
        else -> throw IllegalStateException("Unknown coupon code: $coupon")
    }

    private fun applyDiscount(subtotal: Double, discount: Double): Double = subtotal * (1 - discount)

    /** Added in 1.0.1 so the R8 mapping (and therefore its UUID) differs from 1.0.0. */
    fun loyaltyPoints(cart: Cart): Int = if (cart.items > 2) cart.items * 10 else 0
}
