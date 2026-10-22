const revealItems = document.querySelectorAll(".reveal");

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) {
      return;
    }

    entry.target.classList.add("is-visible");
    observer.unobserve(entry.target);
  });
}, { threshold: 0.16 });

revealItems.forEach((item) => observer.observe(item));

const paletteToggle = document.getElementById("paletteToggle");

paletteToggle?.addEventListener("click", () => {
  document.body.classList.toggle("alt-palette");
});
