.pragma library

// Formatting for the bar widget and panel. Pure functions over the engine's
// snapshot (see daemon/dmd.py public() and snapshot()), plus syncRows(),
// which keeps the panel's list in step with it.

function bytes(n) {
  n = Number(n) || 0
  var units = ["B", "KB", "MB", "GB", "TB"]
  var i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n < 10 ? 1 : 0)) + " " + units[i]
}

function speed(n) {
  return bytes(n) + "/s"
}

function duration(seconds) {
  seconds = Math.max(0, Math.round(Number(seconds) || 0))
  if (seconds < 60) return seconds + "s"
  if (seconds < 3600) return Math.floor(seconds / 60) + "m " + pad(seconds % 60) + "s"
  return Math.floor(seconds / 3600) + "h " + pad(Math.floor(seconds % 3600 / 60)) + "m"
}

function pad(n) {
  return n < 10 ? "0" + n : String(n)
}

function fraction(job) {
  if (!job || !job.total) return -1
  return Math.max(0, Math.min(1, job.completed / job.total))
}

function percent(f) {
  return Math.floor(f * 100) + "%"
}

function unfinished(status) {
  return ["queued", "active", "checking", "paused", "retrying", "finishing"].indexOf(status) >= 0
}

// The server can't continue this download from the middle: a pause or a
// dropped connection starts it over.
function cantResume(job) {
  return job && job.resumable === false && unfinished(job.status)
}

// One line under a job's name.
function statusLine(job, now) {
  if (cantResume(job) && job.status === "paused")
    return "Paused · this link can't resume, so it will start over"
  var line = baseStatus(job, now)
  return cantResume(job) ? line + " · can't resume" : line
}

function baseStatus(job, now) {
  var f = fraction(job)
  var size = job.total ? bytes(job.completed) + " of " + bytes(job.total) : bytes(job.completed)
  switch (job.status) {
  case "active":
    var parts = [f >= 0 ? percent(f) : bytes(job.completed), speed(job.speed)]
    if (job.eta !== null && job.eta !== undefined) parts.push(duration(job.eta) + " left")
    return parts.join(" · ")
  case "queued":
    return job.completed > 0 ? "Queued · " + size : "Queued"
  case "checking":
    return "Checking the file on the server…"
  case "finishing":
    return "Finishing…"
  case "paused":
    return "Paused · " + (f >= 0 ? percent(f) + " · " : "") + size
  case "retrying":
    var why = job.error && job.error.text ? job.error.text : "Interrupted"
    if (job.waiting_network) return why + " · waiting for the network"
    var wait = job.next_retry ? Math.max(0, job.next_retry - now) : 0
    return why + " · retrying in " + duration(wait)
  case "done":
    return "Done · " + bytes(job.total || job.completed)
  case "failed":
    var err = job.error || {}
    return "Failed · " + (err.text || "unknown error")
  case "cancelled":
    return "Cancelled"
  }
  return job.status || ""
}

function heroLine(service) {
  if (!service) return ""
  switch (service.engineState) {
  case "missing": return "Not set up yet"
  case "offline": return "Engine stopped"
  case "engine": return "Engine problem"
  }
  var parts = []
  if (service.downloading > 0) parts.push(service.downloading + " downloading")
  if (service.waiting > 0) parts.push(service.waiting + " waiting")
  if (service.pausedCount > 0) parts.push(service.pausedCount + " paused")
  if (service.speed > 0) parts.push(speed(service.speed))
  if (service.speedLimit && service.speedLimit !== "0") parts.push("limit " + limitLabel(service.speedLimit))
  if (!service.online) parts.push("offline")
  return parts.length > 0 ? parts.join(" · ") : "Nothing downloading"
}

// "2M" -> "2 MB/s"
function limitLabel(value) {
  var m = String(value).match(/^(\d+(?:\.\d+)?)([KkMm]?)$/)
  if (!m) return value
  return m[1] + " " + (m[2] ? m[2].toUpperCase() + "B" : "B") + "/s"
}

// The speed-limit choices, plus the current one if it was set elsewhere.
function limitOptions(current) {
  var options = [
    { value: "0", label: "Off" },
    { value: "1M", label: "1 MB/s" },
    { value: "5M", label: "5 MB/s" },
    { value: "20M", label: "20 MB/s" }
  ]
  var known = options.some(function(o) { return o.value === current })
  if (!known && current) options.push({ value: current, label: limitLabel(current) })
  return options
}

// Video pages get a film glyph in front of their title.
function displayName(job) {
  var name = job.name || job.url || ""
  return job.kind === "media" ? "󰕧  " + name : name
}

function tooltip(service) {
  if (!service) return "Downloads"
  var line = heroLine(service)
  if (service.engineState === "ok" && service.progress >= 0 && service.busyCount > 0)
    line += " · " + percent(service.progress)
  if (service.failedCount > 0) line += " · " + service.failedCount + " failed"
  return "Downloads: " + line
}

function engineMessage(service) {
  switch (service.engineState) {
  case "missing":
    return "The download engine isn't installed. Setup installs aria2 and starts the engine as a background service."
  case "offline":
    return "The download engine isn't running, so nothing is downloading."
  case "engine":
    return "aria2 isn't working: " + (service.engineError || "unknown error")
  }
  return ""
}

// Bring `rows` (a ListModel of { jobId }) into the order of `jobs`, touching
// only the rows that came, went or moved. The snapshot is a new array every
// half second while downloading; a list built straight from it recreates
// every row each time, buttons under the pointer included.
function syncRows(rows, jobs) {
  var ids = jobs.map(function(j) { return j.id })
  for (var i = rows.count - 1; i >= 0; i--) {
    if (ids.indexOf(rows.get(i).jobId) < 0) rows.remove(i)
  }
  for (var k = 0; k < ids.length; k++) {
    var at = -1
    for (var r = k; r < rows.count; r++) {
      if (rows.get(r).jobId === ids[k]) { at = r; break }
    }
    if (at < 0) rows.insert(k, { jobId: ids[k] })
    else if (at !== k) rows.move(at, k, 1)
  }
}
