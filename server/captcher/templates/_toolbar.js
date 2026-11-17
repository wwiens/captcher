
<script>
  (function () {
    var h = document.querySelector(".site-header");
    if (!h) return;
    var onScroll = function () { h.classList.toggle("scrolled", window.scrollY > 10); };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  })();
</script>
