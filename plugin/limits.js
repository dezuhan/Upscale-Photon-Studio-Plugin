/*
 * Photon pixel-transfer limits, shared by the panel and the headless tests.
 *
 * Photon's images.encode / images.decode / documents.pixels pipeline tops out at 16 MP and 8192 px
 * per side. Every place that turns a layer into an RGBA buffer has to respect that, so the checks
 * live here instead of being duplicated inline.
 */
(function (root, factory) {
  const mod = factory();
  if (typeof module === "object" && module.exports) module.exports = mod;
  else root.PhotonLimits = mod;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MAX_EDGE = 8192;
  const MAX_PIXELS = 16 * 1024 * 1024;

  function megapixels(value) {
    return Math.round((value / 1048576) * 10) / 10;
  }

  /** The source layer must fit the transfer limit. */
  function checkSource(width, height, limits) {
    const maxPixels = (limits && limits.maxPixels) || MAX_PIXELS;
    if (!width || !height) {
      throw new Error("This layer has no raster pixels. Rasterize text or vector layers first.");
    }
    if (width * height > maxPixels) {
      throw new Error(
        "Layer is " + width + "x" + height + ", above the " + maxPixels / 1048576 + " MP limit.",
      );
    }
  }

  return { MAX_EDGE, MAX_PIXELS, megapixels, checkSource };
});
