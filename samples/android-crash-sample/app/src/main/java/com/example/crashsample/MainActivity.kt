package com.example.crashsample

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import io.sentry.Sentry

/**
 * Minimal crash playground. Buttons are wired to code in obfuscated classes so
 * that a stack trace without mapping.txt is unreadable and one with it is not.
 *
 * Automation: `adb shell am start -n com.example.crashsample/.MainActivity --es action crash`
 * triggers the same paths without tapping (actions: crash, npe, handled, anr).
 */
class MainActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        root.addView(TextView(this).apply {
            text = "Sentry crash sample\nrelease build, R8 enabled"
            textSize = 18f
        })
        root.addView(button("Crash: IllegalStateException in OrderProcessor") { OrderProcessor().checkout(Cart(items = 3, coupon = "BROKEN")) })
        root.addView(button("Crash: NullPointerException") { OrderProcessor().receiptFor(null) })
        root.addView(button("Handled exception (Sentry.captureException)") {
            try {
                OrderProcessor().checkout(Cart(items = 0, coupon = null))
            } catch (e: Exception) {
                Sentry.captureException(e)
            }
        })
        root.addView(button("ANR: block main thread 8s") { Thread.sleep(8_000) })
        setContentView(root)

        intent?.getStringExtra("action")?.let { action ->
            Handler(Looper.getMainLooper()).postDelayed({ runAction(action) }, 1_500)
        }
    }

    private fun runAction(action: String) {
        when (action) {
            "crash" -> OrderProcessor().checkout(Cart(items = 3, coupon = "BROKEN"))
            "npe" -> OrderProcessor().receiptFor(null)
            "handled" -> try {
                OrderProcessor().checkout(Cart(items = 0, coupon = null))
            } catch (e: Exception) {
                Sentry.captureException(e)
            }
            "anr" -> Thread.sleep(8_000)
        }
    }

    private fun button(label: String, onClick: () -> Unit): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            setOnClickListener { onClick() }
        }
}
