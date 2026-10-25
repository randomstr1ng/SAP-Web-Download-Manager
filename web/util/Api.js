sap.ui.define([], function () {
	"use strict";

	/**
	 * Thin API + WebSocket client for the SAP Download Manager backend.
	 * All fetches are plain HTTP (internal-only decision) and expect JSON.
	 */
	var Api = {
		model: null,
		_ws: null,
		_wsToken: null,

		// ---------------------------------------------------------------- HTTP
		call: function (sMethod, sPath, oBody) {
			return fetch(sPath, {
				method: sMethod,
				headers: { "Content-Type": "application/json" },
				body: oBody ? JSON.stringify(oBody) : undefined,
				credentials: "same-origin"
			}).then(function (res) {
				if (res.status === 204) { return {}; }
				return res.json().then(function (data) {
					if (!res.ok) {
						throw new Error(data.detail || ("HTTP " + res.status));
					}
					return data;
				});
			});
		},
		get: function (sPath) { return this.call("GET", sPath); },
		post: function (sPath, oBody) { return this.call("POST", sPath, oBody || {}); },
		put: function (sPath, oBody) { return this.call("PUT", sPath, oBody); },
		del: function (sPath) { return this.call("DELETE", sPath); },

		/** Poll a background task until done/error. */
		waitTask: function (sTaskId, fnOnProgress) {
			var that = this;
			return new Promise(function (resolve, reject) {
				var iCount = 0;
				(function poll() {
					that.get("/api/tasks/" + sTaskId).then(function (task) {
						if (fnOnProgress) { fnOnProgress(task); }
						if (task.state === "done") { return resolve(task); }
						if (task.state === "error") { return reject(new Error(task.error)); }
						if (++iCount > 1200) { return reject(new Error("Task timed out")); }
						window.setTimeout(poll, 500);
					}).catch(reject);
				}());
			});
		},

		// ------------------------------------------------------------ WebSocket
		_wsUrl: function () {
			var sProto = window.location.protocol === "https:" ? "wss" : "ws";
			var sUrl = sProto + "://" + window.location.host + "/ws";
			if (this._wsToken) { sUrl += "?token=" + encodeURIComponent(this._wsToken); }
			return sUrl;
		},

		connectEvents: function (oModel) {
			var that = this;
			this.model = oModel;
			var openWs = function () {
				that._ws = new WebSocket(that._wsUrl());
				that._ws.onmessage = function (evt) {
					var oEvent;
					try { oEvent = JSON.parse(evt.data); } catch (e) { return; }
					that._handleEvent(oEvent);
				};
				that._ws.onclose = function (evt) {
					if (evt.code === 4001) { // admin token required
						that._wsToken = window.prompt(
							"This server requires an access token (admin password):");
						if (that._wsToken) { window.setTimeout(openWs, 100); }
						return;
					}
					window.setTimeout(openWs, 5000); // reconnect with backoff
				};
			};
			openWs();
		},

		_handleEvent: function (oEvent) {
			var oModel = this.model;
			if (!oModel) { return; }
			if (oEvent.type === "progress") {
				var oProgress = oModel.getProperty("/progress") || {};
				var oItem = oProgress[oEvent.dockey] || {};
				oItem.bytes = oEvent.bytes_committed;
				oItem.total = oEvent.total_size || 0;
				oItem.speed = oEvent.speed_bps || 0;
				oItem.component = oEvent.comp_nr + "/" + oEvent.comp_total;
				oItem.file = oEvent.file_name;
				oProgress[oEvent.dockey] = oItem;
				oModel.setProperty("/progress", oProgress);

				// Live-update the basket row so the ProgressIndicator moves
				// while the download is running (no basket refresh needed).
				var aItems = oModel.getProperty("/basket/items") || [];
				var iPct = oItem.total > 0
					? Math.min(100, Math.round(100 * oItem.bytes / oItem.total))
					: 100;
				var sPct = iPct + " %";
				var sStatus = sap.ui.getCore().getModel("i18n")
					.getResourceBundle().getText("downloadingPercent", [iPct]);
				for (var i = 0; i < aItems.length; i++) {
					if (aItems[i].dockey === oEvent.dockey) {
						oModel.setProperty("/basket/items/" + i + "/progress_percent", iPct);
						oModel.setProperty("/basket/items/" + i + "/progress_text", sPct);
						oModel.setProperty("/basket/items/" + i + "/status_text", sStatus);
						oModel.setProperty("/basket/items/" + i + "/downloading", true);
						break;
					}
				}
			} else if (oEvent.type === "status") {
				if (oEvent.status) { oModel.setProperty("/status", oEvent.status); }
				if (oEvent.action) { oModel.setProperty("/action", oEvent.action); }
				this.refreshBasket();
			} else if (oEvent.type === "error") {
				oModel.setProperty("/lastError", oEvent.message);
			}
		},

		refreshBasket: function () {
			var that = this;
			return this.get("/api/basket").then(function (oBasket) {
				that.model.setProperty("/basket", oBasket);
			}).catch(function () { /* not refreshed yet */ });
		}
	};

	return Api;
});
