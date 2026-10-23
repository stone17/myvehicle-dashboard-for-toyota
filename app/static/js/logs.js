document.addEventListener('DOMContentLoaded', () => {
    const logContent = document.getElementById('log-content');
    const MAX_LOG_LINES = 1000; // Maximum number of log lines to display

    if (!logContent) {
        console.error("Log page elements not found.");
        return;
    }

    function connectToLogStream() {
        logContent.textContent = 'Connecting to log stream...';
        let isConnectingPlaceholder = true;
        let pendingLogs = [];
        let rafScheduled = false;

        const eventSource = new EventSource('/api/logs');

        function createLogLineElement(logData) {
            const logLine = document.createElement('div');
            logLine.className = `log-line ${logData.level || 'INFO'}`;
            logLine.textContent = logData.message;
            return logLine;
        }

        function isNearBottom() {
            const threshold = 40;
            return logContent.scrollHeight - logContent.scrollTop - logContent.clientHeight <= threshold;
        }

        function flushPendingLogs() {
            rafScheduled = false;
            if (pendingLogs.length === 0) return;

            const shouldAutoScroll = isConnectingPlaceholder || isNearBottom();

            if (isConnectingPlaceholder) {
                logContent.innerHTML = '';
                isConnectingPlaceholder = false;
            }

            const batch = pendingLogs;
            pendingLogs = [];

            // If the incoming batch alone exceeds MAX_LOG_LINES, only render the tail
            const startIdx = Math.max(0, batch.length - MAX_LOG_LINES);
            const fragment = document.createDocumentFragment();
            for (let i = startIdx; i < batch.length; i++) {
                fragment.appendChild(createLogLineElement(batch[i]));
            }
            logContent.appendChild(fragment);

            // Trim old log lines if total exceeds MAX_LOG_LINES
            while (logContent.childElementCount > MAX_LOG_LINES) {
                logContent.removeChild(logContent.firstElementChild);
            }

            if (shouldAutoScroll) {
                logContent.scrollTop = logContent.scrollHeight;
            }
        }

        function scheduleFlush() {
            if (!rafScheduled) {
                rafScheduled = true;
                requestAnimationFrame(flushPendingLogs);
            }
        }

        eventSource.addEventListener('history', function(event) {
            try {
                const history = JSON.parse(event.data);
                logContent.innerHTML = '';
                isConnectingPlaceholder = false;
                pendingLogs = [];

                const startIdx = Math.max(0, history.length - MAX_LOG_LINES);
                const fragment = document.createDocumentFragment();
                for (let i = startIdx; i < history.length; i++) {
                    fragment.appendChild(createLogLineElement(history[i]));
                }

                logContent.appendChild(fragment);
                logContent.scrollTop = logContent.scrollHeight;
            } catch (e) {
                console.error("Failed to parse log history:", event.data, e);
                logContent.textContent = 'Error loading log history.';
            }
        });

        eventSource.addEventListener('message', function(event) {
            try {
                const logData = JSON.parse(event.data);
                pendingLogs.push(logData);
                scheduleFlush();
            } catch (e) {
                console.error("Failed to parse log data:", event.data, e);
            }
        });

        eventSource.onerror = function() {
            pendingLogs.push({
                level: 'WARNING',
                message: '--- Connection to log stream lost. Reconnecting... ---'
            });
            scheduleFlush();
        };

        window.addEventListener('pagehide', () => eventSource.close());
        window.addEventListener('beforeunload', () => eventSource.close());
    }

    // Start the connection
    connectToLogStream();

    const saveRawCheckbox = document.getElementById('save-raw-responses');
    const rawRetentionGroup = document.getElementById('raw-retention-group');
    const rawRetentionSelect = document.getElementById('raw-responses-retention');
    const rawViewerPanel = document.getElementById('raw-viewer-panel');
    const rawPollSelector = document.getElementById('raw-poll-selector');
    const refreshRawBtn = document.getElementById('refresh-raw-btn');
    const downloadPollLink = document.getElementById('download-poll-link');
    const rawFilesContainer = document.getElementById('raw-files-container');
    const rawFilesDescriptor = document.getElementById('raw-files-descriptor');
    const rawContent = document.getElementById('raw-response-content');

    let allPolls = [];
    let appConfig = {};
    let modalMap = null;
    let mapLayers = [];

    let cachedDateFormatter = null;
    let cachedFormatterTz = null;

    function formatTZDate(dateObj) {
        const tz = appConfig?.timezone || 'UTC';
        try {
            if (!cachedDateFormatter || cachedFormatterTz !== tz) {
                cachedDateFormatter = new Intl.DateTimeFormat('en-GB', {
                    timeZone: tz,
                    year: '2-digit',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: false
                });
                cachedFormatterTz = tz;
            }
            const parts = cachedDateFormatter.formatToParts(dateObj);
            const p = {};
            parts.forEach(part => p[part.type] = part.value);
            return `${p.year}/${p.month}/${p.day}-${p.hour}:${p.minute}`;
        } catch (e) {
            const yy = String(dateObj.getFullYear()).slice(2);
            const mm = String(dateObj.getMonth() + 1).padStart(2, '0');
            const dd = String(dateObj.getDate()).padStart(2, '0');
            const hh = String(dateObj.getHours()).padStart(2, '0');
            const mins = String(dateObj.getMinutes()).padStart(2, '0');
            return `${yy}/${mm}/${dd}-${hh}:${mins}`;
        }
    }

    async function fetchRawPollsList() {
        try {
            const res = await fetch('/api/raw_responses');
            if (!res.ok) throw new Error('Failed to fetch polls list');
            allPolls = await res.json();
            
            // Keep the default option
            const defaultOption = rawPollSelector.querySelector('option[disabled]');
            rawPollSelector.innerHTML = '';
            const fragment = document.createDocumentFragment();
            if (defaultOption) fragment.appendChild(defaultOption);
            
            allPolls.forEach(poll => {
                const opt = document.createElement('option');
                opt.value = poll.poll_id;
                
                const d = new Date(poll.mtime * 1000);
                const dateStr = formatTZDate(d);
                
                const match = poll.poll_id.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_(.+)$/);
                let typeStr = match ? match[7] : poll.poll_id;
                
                if (typeStr === 'fetch' || typeStr === 'backfill_trips') {
                    const count = poll.trip_count || 0;
                    opt.textContent = `${dateStr} (${count} trips)`;
                } else {
                    if (match) {
                        typeStr = typeStr.replace(/_/g, ' ');
                        typeStr = typeStr.charAt(0).toUpperCase() + typeStr.slice(1);
                        opt.textContent = `${dateStr} (${typeStr})`;
                    } else {
                        opt.textContent = `${dateStr} (${poll.poll_id})`;
                    }
                }
                fragment.appendChild(opt);
            });
            rawPollSelector.appendChild(fragment);
        } catch (e) {
            console.error(e);
        }
    }

    function extractCoordinates(trip) {
        const start = (trip.start_lat && trip.start_lon) ? { lat: trip.start_lat, lon: trip.start_lon } : 
                      (trip.summary && trip.summary.startLat && trip.summary.startLon ? { lat: trip.summary.startLat, lon: trip.summary.startLon } : 
                      (trip.summary && trip.summary.start_lat && trip.summary.start_lon ? { lat: trip.summary.start_lat, lon: trip.summary.start_lon } :
                      (trip.locations && trip.locations.start ? { lat: trip.locations.start.lat, lon: trip.locations.start.lon } : null)));
        const end = (trip.end_lat && trip.end_lon) ? { lat: trip.end_lat, lon: trip.end_lon } : 
                    (trip.summary && trip.summary.endLat && trip.summary.endLon ? { lat: trip.summary.endLat, lon: trip.summary.endLon } : 
                    (trip.summary && trip.summary.end_lat && trip.summary.end_lon ? { lat: trip.summary.end_lat, lon: trip.summary.end_lon } :
                    (trip.locations && trip.locations.end ? { lat: trip.locations.end.lat, lon: trip.locations.end.lon } : null)));
        return { start, end };
    }

    function extractTripsFromRawJson(data) {
        if (!data) return [];
        
        // Detect route-only files
        if (data.payload && data.payload.route && Array.isArray(data.payload.route)) {
            const routeArray = data.payload.route;
            const startPt = routeArray[0];
            const endPt = routeArray[routeArray.length - 1];
            return [{
                id: "Route File",
                start_time: startPt ? startPt.ts : null,
                route: routeArray,
                summary: {
                    startLat: startPt ? startPt.lat : null,
                    startLon: startPt ? startPt.lon : null,
                    endLat: endPt ? endPt.lat : null,
                    endLon: endPt ? endPt.lon : null,
                    distance: 0
                }
            }];
        } else if (data.route && Array.isArray(data.route)) {
            const routeArray = data.route;
            const startPt = routeArray[0];
            const endPt = routeArray[routeArray.length - 1];
            return [{
                id: "Route File",
                start_time: startPt ? startPt.ts : null,
                route: routeArray,
                summary: {
                    startLat: startPt ? startPt.lat : null,
                    startLon: startPt ? startPt.lon : null,
                    endLat: endPt ? endPt.lat : null,
                    endLon: endPt ? endPt.lon : null,
                    distance: 0
                }
            }];
        } else if (Array.isArray(data) && data.length > 0 && data[0].lat !== undefined && data[0].lon !== undefined) {
            const startPt = data[0];
            const endPt = data[data.length - 1];
            return [{
                id: "Route File",
                start_time: startPt ? startPt.ts : null,
                route: data,
                summary: {
                    startLat: startPt ? startPt.lat : null,
                    startLon: startPt ? startPt.lon : null,
                    endLat: endPt ? endPt.lat : null,
                    endLon: endPt ? endPt.lon : null,
                    distance: 0
                }
            }];
        }

        const isTrip = (obj) => {
            if (!obj || typeof obj !== 'object') return false;
            if (obj.start_time || obj.start_timestamp) return true;
            if (obj.summary && (obj.summary.startTs || obj.summary.start_ts || obj.summary.start_time || obj.summary.start_timestamp)) return true;
            return false;
        };

        if (data.payload && Array.isArray(data.payload.trips)) {
            return data.payload.trips;
        }
        if (data.payload && isTrip(data.payload)) {
            return [data.payload];
        }
        if (Array.isArray(data)) {
            return data.filter(isTrip);
        }
        if (isTrip(data)) {
            return [data];
        }
        return [];
    }

    function getTripStartTimestamp(trip) {
        return trip.start_time || 
               trip.start_timestamp || 
               (trip.summary && (trip.summary.startTs || trip.summary.start_ts || trip.summary.start_timestamp));
    }

    function initMapModal() {
        const mapModal = document.getElementById('map-modal');
        const closeMapModal = document.getElementById('close-map-modal');
        
        closeMapModal.addEventListener('click', () => {
            mapModal.style.display = 'none';
        });

        window.showTripsOnMap = function(trips) {
            mapModal.style.display = 'flex';
            
            if (!modalMap) {
                modalMap = L.map('modal-map').setView([0, 0], 2);
                L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                    attribution: '&copy; OpenStreetMap contributors'
                }).addTo(modalMap);
            }
            
            // Clear existing layers
            mapLayers.forEach(layer => modalMap.removeLayer(layer));
            mapLayers = [];
            
            const bounds = L.latLngBounds();
            const colors = ['#007bff', '#28a745', '#dc3545', '#fd7e14', '#6f42c1'];
            
            trips.forEach((trip, index) => {
                let route = trip.route || (trip._trip && trip._trip.route);
                let isFallback = false;
                if (!route || !Array.isArray(route) || route.length === 0) {
                    const coords = extractCoordinates(trip);
                    const start = coords.start;
                    const end = coords.end;
                    if (start && end && start.lat !== undefined && start.lon !== undefined && end.lat !== undefined && end.lon !== undefined) {
                        route = [start, end];
                        isFallback = true;
                    }
                }
                if (!route || !Array.isArray(route) || route.length === 0) return;
                
                const latLngs = route.map(pt => [pt.lat, pt.lon]).filter(pt => pt[0] !== undefined && pt[1] !== undefined);
                if (latLngs.length === 0) return;
                
                const color = colors[index % colors.length];
                const polylineOptions = { color: color, weight: 4, opacity: 0.8 };
                if (isFallback) {
                    polylineOptions.dashArray = '5, 10';
                }
                const polyline = L.polyline(latLngs, polylineOptions).addTo(modalMap);
                mapLayers.push(polyline);
                
                latLngs.forEach(ll => bounds.extend(ll));
                
                // Markers
                const startMarker = L.circleMarker(latLngs[0], { radius: 6, fillColor: 'green', color: 'white', weight: 2, fillOpacity: 1 }).addTo(modalMap);
                const endMarker = L.circleMarker(latLngs[latLngs.length - 1], { radius: 6, fillColor: 'red', color: 'white', weight: 2, fillOpacity: 1 }).addTo(modalMap);
                mapLayers.push(startMarker, endMarker);
            });
            
            if (bounds.isValid()) {
                // setTimeout needed because map container size might not be computed yet
                setTimeout(() => {
                    modalMap.invalidateSize();
                    modalMap.fitBounds(bounds, { padding: [30, 30] });
                }, 100);
            }
        };
    }
    initMapModal();

    function truncateJsonData(data) {
        if (Array.isArray(data)) {
            if (data.length > 5) {
                const truncated = data.slice(0, 5).map(truncateJsonData);
                truncated.push(`... and ${data.length - 5} more items truncated ...`);
                return truncated;
            }
            return data.map(truncateJsonData);
        } else if (data !== null && typeof data === 'object') {
            const result = {};
            for (const key in data) {
                result[key] = truncateJsonData(data[key]);
            }
            return result;
        }
        return data;
    }

    async function loadRawFile(pollId, filename, itemEl) {
        if (itemEl) {
            document.querySelectorAll('.raw-file-item').forEach(el => el.classList.remove('active'));
            itemEl.classList.add('active');
        }
        
        const url = `/api/raw_responses/${encodeURIComponent(pollId)}/${encodeURIComponent(filename)}`;
        
        let fileSize = 0;
        const poll = allPolls.find(p => p.poll_id === pollId);
        if (poll) {
            const fileObj = poll.files.find(f => f.filename === filename);
            if (fileObj) {
                if (fileObj.size) fileSize = fileObj.size;
                if (rawFilesDescriptor && fileObj.mtime) {
                    const d = new Date(fileObj.mtime * 1000);
                    const tz = appConfig?.timezone || 'UTC';
                    rawFilesDescriptor.textContent = window.safeLocaleString(d, undefined, { timeZone: tz });
                    rawFilesDescriptor.style.display = 'block';
                } else if (rawFilesDescriptor) {
                    rawFilesDescriptor.style.display = 'none';
                }
            }
        }

        const tripsSummaryPanel = document.getElementById('trips-summary-panel');
        const tripsSummaryBody = document.getElementById('trips-summary-body');
        const showAllMapBtn = document.getElementById('show-all-map-btn');
        
        async function processAndRenderTrips(data) {
            if (poll && poll.metadata && poll.metadata.updates && (!poll.metadata.updates.trips || poll.metadata.updates.trips.length === 0)) {
                if (tripsSummaryPanel) tripsSummaryPanel.style.display = 'none';
                return;
            }
            
            const lowerFilename = filename.toLowerCase();
            if (!lowerFilename.includes('trip') && !lowerFilename.includes('route') && !lowerFilename.includes('data')) {
                if (tripsSummaryPanel) tripsSummaryPanel.style.display = 'none';
                return;
            }

            const trips = extractTripsFromRawJson(data);
            if (!trips || trips.length === 0) {
                if (tripsSummaryPanel) tripsSummaryPanel.style.display = 'none';
                return;
            }
            
            if (tripsSummaryPanel) tripsSummaryPanel.style.display = 'block';
            if (tripsSummaryBody) tripsSummaryBody.innerHTML = '<tr><td colspan="4" style="text-align: center; padding: 10px;">Checking database...</td></tr>';
            
            const checkItems = trips.map(t => ({
                id: t.id || null,
                start_timestamp: getTripStartTimestamp(t) || null
            }));
            
            let dbStatus = {};
            try {
                const res = await fetch('/api/raw_responses/check_trips', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ items: checkItems })
                });
                if (res.ok) {
                    dbStatus = await res.json();
                }
            } catch (e) {
                console.error("Failed to check trips against DB:", e);
            }
            
            if (tripsSummaryBody) tripsSummaryBody.innerHTML = '';
            let mapableTrips = [];
            
            const isImperial = appConfig?.unit_system?.startsWith('imperial');
            
            trips.forEach(trip => {
                const id = trip.id;
                const startTs = getTripStartTimestamp(trip);
                const key = id || startTs;
                const exists = dbStatus[key];
                
                const tr = document.createElement('tr');
                tr.style.borderBottom = '1px solid var(--border-color)';
                
                // Time
                const timeTd = document.createElement('td');
                timeTd.style.padding = '8px';
                timeTd.textContent = startTs ? formatTZDate(new Date(startTs)) : 'Unknown';
                tr.appendChild(timeTd);
                
                // Distance
                const distTd = document.createElement('td');
                distTd.style.padding = '8px';
                let distKm = trip.distance || (trip.summary && trip.summary.distance);
                if (distKm === undefined && trip.summary && trip.summary.length !== undefined) {
                    distKm = trip.summary.length / 1000;
                }
                distKm = distKm || 0;
                if (isImperial) {
                    distTd.textContent = (distKm * 0.621371).toFixed(1) + ' mi';
                } else {
                    distTd.textContent = distKm.toFixed(1) + ' km';
                }
                tr.appendChild(distTd);
                
                // Status
                const statusTd = document.createElement('td');
                statusTd.style.padding = '8px';
                if (exists) {
                    statusTd.innerHTML = '<span style="color: #28a745;">✔ In Database</span>';
                } else {
                    statusTd.innerHTML = '<span style="color: #dc3545;">❌ Missing</span>';
                }
                tr.appendChild(statusTd);
                
                // Action
                const actionTd = document.createElement('td');
                actionTd.style.padding = '8px';
                const mapBtn = document.createElement('button');
                mapBtn.className = 'btn secondary-btn';
                mapBtn.style.padding = '2px 8px';
                mapBtn.textContent = 'Map';
                
                let route = trip.route || (trip._trip && trip._trip.route);
                if (!route || !Array.isArray(route) || route.length === 0) {
                    const coords = extractCoordinates(trip);
                    const start = coords.start;
                    const end = coords.end;
                    if (start && end && start.lat !== undefined && start.lon !== undefined && end.lat !== undefined && end.lon !== undefined) {
                        route = [start, end];
                        trip.route = route; // Assign back so map rendering works
                    }
                }
                if (route && Array.isArray(route) && route.length > 0) {
                    mapableTrips.push(trip);
                    mapBtn.onclick = () => window.showTripsOnMap([trip]);
                } else {
                    mapBtn.disabled = true;
                }
                actionTd.appendChild(mapBtn);
                tr.appendChild(actionTd);
                
                if (tripsSummaryBody) tripsSummaryBody.appendChild(tr);
            });
            
            if (showAllMapBtn) {
                showAllMapBtn.disabled = mapableTrips.length === 0;
                showAllMapBtn.onclick = () => window.showTripsOnMap(mapableTrips);
            }
        }

        if (fileSize > 150 * 1024) {
            const kbSize = (fileSize / 1024).toFixed(1);
            rawContent.innerHTML = `
                <div style="background: var(--card-bg); border: 1px solid var(--border-color); padding: 20px; border-radius: 8px; max-width: 600px; margin: 20px auto; text-align: center; font-family: sans-serif;">
                    <h3 style="color: #ff9800; margin-top: 0;">Large File Warning</h3>
                    <p style="margin-bottom: 20px;">This file is very large (${kbSize} KB). Rendering the full JSON may freeze your browser.</p>
                    <div style="display: flex; gap: 10px; justify-content: center; flex-wrap: wrap;">
                        <button id="btn-render-truncated" class="btn" style="padding: 8px 16px; cursor: pointer; background: #007bff; color: white; border: none; border-radius: 4px;">Render Truncated (Fast)</button>
                        <button id="btn-render-full" class="btn" style="padding: 8px 16px; cursor: pointer; background: var(--border-color); color: var(--text-color); border: none; border-radius: 4px;">Render Full (Slow)</button>
                        <a id="btn-download-raw" class="btn" style="padding: 8px 16px; text-decoration: none; background: var(--border-color); color: var(--text-color); border-radius: 4px;">Download File</a>
                    </div>
                </div>
            `;

            const downloadLink = document.getElementById('btn-download-raw');
            if (downloadLink) {
                downloadLink.href = url;
                downloadLink.download = filename;
            }

            document.getElementById('btn-render-truncated').addEventListener('click', async () => {
                rawContent.textContent = 'Loading...';
                try {
                    const res = await fetch(url);
                    if (!res.ok) throw new Error('Fetch error.');
                    const data = await res.json();
                    rawContent.textContent = JSON.stringify(truncateJsonData(data), null, 2);
                } catch (e) {
                    rawContent.textContent = e.message;
                }
            });

            document.getElementById('btn-render-full').addEventListener('click', async () => {
                rawContent.textContent = 'Loading...';
                try {
                    const res = await fetch(url);
                    if (!res.ok) throw new Error('Fetch error.');
                    const data = await res.json();
                    rawContent.textContent = JSON.stringify(data, null, 2);
                    processAndRenderTrips(data);
                } catch (e) {
                    rawContent.textContent = e.message;
                }
            });
        } else {
            try {
                const res = await fetch(url);
                if (!res.ok) throw new Error(res.status === 404 ? 'File not found.' : 'Fetch error.');
                const data = await res.json();
                rawContent.textContent = JSON.stringify(data, null, 2);
                processAndRenderTrips(data);
            } catch (e) {
                rawContent.textContent = e.message;
            }
        }
    }

    if (saveRawCheckbox) {
        saveRawCheckbox.addEventListener('change', () => {
            const isChecked = saveRawCheckbox.checked;
            rawRetentionGroup.style.display = isChecked ? 'block' : 'none';
            rawViewerPanel.style.display = isChecked ? 'flex' : 'none';
            if (isChecked) fetchRawPollsList();
        });
        rawPollSelector.addEventListener('change', () => {
            const pollId = rawPollSelector.value;
            rawFilesContainer.innerHTML = '';
            rawContent.textContent = '';
            
            const poll = allPolls.find(p => p.poll_id === pollId);
            if (poll && poll.files.length > 0) {
                downloadPollLink.style.display = 'inline-block';
                downloadPollLink.href = `/api/raw_responses/${encodeURIComponent(pollId)}/download`;
                
                poll.files.forEach((file, index) => {
                    const item = document.createElement('div');
                    item.className = 'raw-file-item';
                    
                    let displayName = file.filename;
                    const nameMatch = file.filename.match(/^\d{8}_\d{6}_(.+?)(?:\.json)?$/);
                    if (nameMatch) {
                        displayName = nameMatch[1];
                    }
                    item.textContent = displayName;
                    
                    item.title = file.filename;
                    item.addEventListener('click', () => loadRawFile(pollId, file.filename, item));
                    rawFilesContainer.appendChild(item);
                    
                    if (index === 0) {
                        loadRawFile(pollId, file.filename, item);
                    }
                });
            } else {
                downloadPollLink.style.display = 'none';
            }
        });
        refreshRawBtn.addEventListener('click', () => {
            fetchRawPollsList().then(() => {
                if (rawPollSelector.value) {
                    rawPollSelector.dispatchEvent(new Event('change'));
                }
            });
        });
    }

    // --- Logging Settings ---
    const saveLogSettingsBtn = document.getElementById('save-log-settings-btn');
    const logSettingsStatusMessage = document.getElementById('log-settings-status-message');

    async function loadLogSettings() {
        try {
            const response = await fetch('/api/config');
            const config = await response.json();
            appConfig = config;

            const logging = config.logging || {};
            const levels = logging.levels || {};
            document.getElementById('app-log-level').value = levels.app || 'INFO';

            document.getElementById('log-history-size').value = config.log_history_size || 200;
            
            if (saveRawCheckbox) {
                saveRawCheckbox.checked = config.save_raw_responses === true;
                rawRetentionGroup.style.display = saveRawCheckbox.checked ? 'block' : 'none';
                rawViewerPanel.style.display = saveRawCheckbox.checked ? 'flex' : 'none';
                if (rawRetentionSelect) {
                    rawRetentionSelect.value = config.raw_responses_retention || 'always';
                }
                if (saveRawCheckbox.checked) fetchRawPollsList();
            }
        } catch (error) {
            console.error(`Failed to load settings: ${error.message}`);
            showMessage(logSettingsStatusMessage, `Failed to load settings: ${error.message}`, 'error');
        }
    }

    saveLogSettingsBtn.addEventListener('click', () => {
        const appLogLevel = document.getElementById('app-log-level').value;
        const logHistorySize = parseInt(document.getElementById('log-history-size').value, 10);

        const newSettings = {
            logging: {
                levels: {
                    app: appLogLevel,
                }
            },
            log_history_size: logHistorySize,
            save_raw_responses: saveRawCheckbox ? saveRawCheckbox.checked : false,
            raw_responses_retention: rawRetentionSelect ? rawRetentionSelect.value : 'always'
        };
        saveConfig(newSettings, logSettingsStatusMessage);
    });

    async function saveConfig(newSettings, messageElement) {
        console.log("Attempting to save new settings:", JSON.stringify(newSettings, null, 2));
        try {
            const response = await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newSettings),
            });
            const result = await response.json();
            console.log("Received response from server:", {
                ok: response.ok,
                status: response.status,
                body: result
            });
            if (response.ok) {
                showMessage(messageElement, result.message, 'success');
            } else {
                throw new Error(result.detail || 'An unknown error occurred.');
            }
        } catch (error) {
            showMessage(messageElement, `Error: ${error.message}`, 'error');
            console.error("Error during saveConfig:", error);
        }
    }

    function showMessage(element, message, type = 'info') {
        element.textContent = message;
        element.className = `status-message ${type}`;
        element.style.display = 'inline-block';
        setTimeout(() => { element.style.display = 'none'; }, 5000);
    }

    loadLogSettings();
});
