sap.ui.define([
	"sap/ui/core/mvc/Controller",
	"sap/ui/model/Filter",
	"sap/ui/model/Sorter",
	"sap/m/MessageBox",
	"sap/m/MessageToast",
	"sap/ui/model/json/JSONModel",
	"sap/ui/core/Fragment",
	"dlm/util/Api",
	"sap/ui/core/library"
], function (Controller, Filter, Sorter, MessageBox, MessageToast, JSONModel, Fragment, Api, coreLibrary) {
	"use strict";

	var SORT_PATHS = {
		sortObject: "text",
		sortSize: "size_kb",
		sortDate: "date_text",
		sortStatus: "status_text"
	};

	return Controller.extend("dlm.controller.Basket", {

		onInit: function () {
			this._oModel = this.getOwnerComponent ? null : sap.ui.getCore().getModel();
			var that = this;
			this._oTable = this.byId("basketTable");
			this._oSorter = { key: "sortObject", descending: false };

			// Theme toggle: the icon shows the mode you switch TO
			// (moon = "switch to dark", sun = "switch to light").
			var sInitial = sap.ui.getCore().getConfiguration().getTheme();
			this.byId("btnTheme").setIcon(
				sInitial === "sap_horizon_dark"
					? "sap-icon://light-mode" : "sap-icon://dark-mode");

			Api.refreshBasket().then(function () { that._decorate(); });

			// Periodic status poll (WS covers live updates; poll is the fallback).
			this._iTimer = window.setInterval(function () { that._pollStatus(); }, 2000);
			this._pollStatus();
			this._checkFirstRun();
		},

		onExit: function () {
			window.clearInterval(this._iTimer);
		},

		_coreModel: function (sName) {
			return sap.ui.getCore().getModel(sName);
		},

		// ------------------------------------------------------------ decoration
		_decorate: function () {
			var oModel = sap.ui.getCore().getModel();
			var aItems = oModel.getProperty("/basket/items") || [];
			var oProgress = oModel.getProperty("/progress") || {};
			aItems.forEach(function (oItem) {
				// Technical line: local file names once known (after a download
				// attempt); the dockey otherwise - items often share the same
				// description text and differ only in size and dockey.
				oItem.tech_text = (oItem.local_files && oItem.local_files.length)
					? oItem.local_files.join(", ")
					: oItem.dockey;
				// Registration date YYYYMMDD + hhmmss -> readable
				var sDate = oItem.reg_date || "";
				var sTime = oItem.reg_time || "";
				oItem.date_text = (sDate.length === 8 && sTime.length === 6)
					? sDate.slice(0, 4) + "-" + sDate.slice(4, 6) + "-" + sDate.slice(6, 8) + " " +
					  sTime.slice(0, 2) + ":" + sTime.slice(2, 4) + ":" + sTime.slice(4, 6)
					: "";
				// Semantic state (ValueState colors, no custom CSS)
				var iLocal = oItem.local_status;
				var iGlobal = oItem.global_status;
				if (oItem.downloading) {
					oItem.status_state = coreLibrary.ValueState.Information;
				} else if (iGlobal === 1 || iGlobal === 2 || iGlobal === 3 || iGlobal === 5) {
					oItem.status_state = coreLibrary.ValueState.Error;
				} else if (iLocal === 1) {
					oItem.status_state = coreLibrary.ValueState.Warning;
				} else if (iLocal === 3) {
					oItem.status_state = coreLibrary.ValueState.Success;
				} else {
					oItem.status_state = coreLibrary.ValueState.None;
				}
				// Direct download button: only when the object finished
				// locally and at least one file is known.
				oItem.can_download = (iLocal === 2 || iLocal === 3)
					&& oItem.local_files && oItem.local_files.length > 0;
				var oP = oProgress[oItem.dockey];
				if (oP && oP.total > 0) {
					oItem.progress_percent = Math.min(100,
						Math.round(100 * oP.bytes / oP.total));
				} else if (oItem.downloading) {
					// Download running but no progress data yet: start at 0 -
					// the live WS progress events will move the bar.
					oItem.progress_percent = 0;
				} else {
					oItem.progress_percent = 0;
				}
				// Percentage text shown inside the progress bar (showValue)
				oItem.progress_text = oItem.progress_percent + " %";
			});
			oModel.setProperty("/basket/items", aItems);
			// Basket summary: "5 objects · 12.4 GB"
			var lKb = 0;
			aItems.forEach(function (oItem) { lKb += oItem.size_kb || 0; });
			oModel.setProperty("/basket/summary", aItems.length
				? this._getText("summary",
					[aItems.length, Api._formatBytes(lKb * 1024)])
				: "");
			this._applyFilterSort();
		},

		_pollStatus: function () {
			var that = this;
			Api.get("/api/state").then(function (oStatus) {
				var oModel = that._coreModel();
				oModel.setProperty("/status", oStatus);
				oModel.setProperty("/footer", {
					icon: oStatus.connected ? "sap-icon://connected" : "sap-icon://disconnected",
					connectionText: oStatus.connected
						? that._getText("connected")
						  + (oStatus.server ? " \u00b7 " + oStatus.server : "")
						: that._getText("disconnected"),
					speedText: "", progressText: "",
				watchText: oStatus.watching ? that._getText("watchingActive") : "",
					downloadingText: (oStatus.downloading || []).length
						? that._getText("downloadingCount", [(oStatus.downloading || []).join(", ")])
						: ""
				});
				Api._updateFooterProgress();
				that.byId("notRefreshedText").setVisible(!oModel.getProperty("/basket/refreshed"));
			}).catch(function () {});
		},

		_getText: function (sKey, aArgs) {
			var sText = sap.ui.getCore().getModel("i18n").getResourceBundle().getText(sKey);
			(aArgs || []).forEach(function (v, i) {
				sText = sText.replace("{" + i + "}", String(v));
			});
			return sText;
		},

		// ------------------------------------------------------------- first run
		_checkFirstRun: function () {
			var that = this;
			Api.get("/api/config").then(function (oConfig) {
				if (!oConfig.complete) {
					that._openWizard();
				} else if (!that._coreModel().getProperty("/basket/refreshed")) {
					that.onRefresh();
				}
			});
		},

		_openWizard: function () {
			var that = this;
			Api.get("/api/config").then(function (oConfig) {
				return Fragment.load({
					name: "dlm.fragment.Wizard",
					controller: that
				}).then(function (oDialog) {
					that._oWizard = oDialog;
					that.getView().addDependent(oDialog);
					// Pre-filled like the original WelcomeFrame (default URL,
					// configured store) - the user mainly enters credentials.
					oDialog.setModel(new JSONModel({
						sap_url: oConfig.sap_url || "https://tech.support.sap.com",
						username: "",
						password: "",
						download_store: oConfig.download_store || ""
					}), "wizard");
					oDialog.open();
				});
			});
		},

		onWizardFinish: function () {
			var oData = this._oWizard.getModel("wizard").getData();
			var that = this;
			Api.put("/api/config", {
				sap_url: String(oData.sap_url || "").trim(), username: String(oData.username || "").trim(),
				download_store: oData.download_store || undefined
			}).then(function () {
				return Api.put("/api/credentials", { sap_password: oData.password });
			}).then(function () {
				MessageToast.show(that._getText("wizardDone"));
				that._oWizard.close();
				that.onRefresh();
			}).catch(function (err) { MessageBox.error(err.message); });
		},

		onWizardCancel: function () {
			if (this._oWizard) { this._oWizard.close(); }
		},

		// ------------------------------------------------------------ basket ops
		onRefresh: function () {
			var that = this;
			this._setBusy(true, this._getText("busyRefreshing"));
			Api.post("/api/basket/refresh").then(function (oRes) {
				return Api.waitTask(oRes.task_id);
			}).then(function () {
				that._setBusy(false);
				return Api.refreshBasket();
			}).then(function () {
				that._decorate();
				that._showUpgrade();
			}).catch(function (err) {
				that._setBusy(false);
				that._showError(err.message, "errBasketRefresh");
			});
		},

		_showUpgrade: function () {
			var oBanner = this.byId("upgradeBanner");
			var oUpgrade = sap.ui.getCore().getModel().getProperty("/basket/upgrade");
			if (oUpgrade && oUpgrade.version) {
				var oBundle = sap.ui.getCore().getModel("i18n").getResourceBundle();
				oBanner.setText(oBundle.getText("upgradeAvailable", [oUpgrade.version]));
				oBanner.setVisible(true);
				this._oUpgrade = oUpgrade;
			} else {
				oBanner.setVisible(false);
			}
		},

		onUpgradeLink: function () {
			if (this._oUpgrade && this._oUpgrade.url) {
				window.open(this._oUpgrade.url, "_blank");
			}
		},

		_selectedKeys: function () {
			return this._oTable.getSelectedItems().map(function (oItem) {
				return oItem.getBindingContext().getProperty("dockey");
			});
		},

		onDownloadSelected: function () {
			var aKeys = this._selectedKeys();
			if (!aKeys.length) {
				return MessageToast.show(this._getText("downloadSelected"));
			}
			this._startDownload(aKeys);
		},

		onDownloadAll: function () {
			var aItems = sap.ui.getCore().getModel().getProperty("/basket/items") || [];
			this._startDownload(aItems.map(function (o) { return o.dockey; }));
		},

		_startDownload: function (aKeys) {
			var that = this;
			// Ask for notification permission while we have a user gesture
			Api.requestNotifyPermission();
			Api.post("/api/downloads/start", { keys: aKeys }).then(function (oRes) {
				// Live updates arrive via WebSocket; poll the task as fallback.
				return Api.waitTask(oRes.task_id, function () {
					Api.refreshBasket().then(function () { that._decorate(); });
				});
			}).then(function () {
				return Api.refreshBasket();
			}).then(function () { that._decorate(); })
			.catch(function (err) { that._showError(err.message); });
		},

		onPause: function () {
			var that = this;
			var oStatus = this._coreModel().getProperty("/status") || {};
			// Nothing downloading but busy => a basket refresh is running:
			// cancel it (original jbtnPause interrupts the basket fetch).
			if (!(oStatus.downloading || []).length && oStatus.busy) {
				Api.post("/api/basket/refresh/cancel").then(function (oRes) {
					if (oRes && oRes.cancelled) {
						MessageToast.show(that._getText("refreshCanceled"));
					}
				});
				return;
			}
			var aKeys = this._selectedKeys().length ? this._selectedKeys()
				: (oStatus.downloading || []);
			Promise.all(aKeys.map(function (k) {
				return Api.post("/api/downloads/" + encodeURIComponent(k) + "/pause");
			})).then(function () {
				return Api.refreshBasket();
			}).then(function () { that._decorate(); })
			.catch(function (err) { that._showError(err.message); });
		},

		onProtocol: function () {
			var aKeys = this._selectedKeys();
			if (!aKeys.length) { return MessageToast.show(this._getText("protocol")); }
			var that = this;
			Api.get("/api/objects/" + encodeURIComponent(aKeys[0]) + "/protocol")
				.then(function (oData) {
					Fragment.load({
						name: "dlm.fragment.Protocol", controller: that
					}).then(function (oDialog) {
						that.getView().addDependent(oDialog);
						oDialog.setModel(new JSONModel({ entries: oData.entries }), "dlg");
						oDialog.open();
					});
				});
		},

		onItemPress: function (oEvent) {
			var oCtx = oEvent.getParameter("listItem").getBindingContext();
			this._openDetail(oCtx.getProperty("dockey"));
		},

		onDetails: function () {
			var aKeys = this._selectedKeys();
			if (!aKeys.length) {
				return MessageToast.show(this._getText("details"));
			}
			this._openDetail(aKeys[0]);
		},

		onDownloadFile: function (oEvent) {
			var oItem = oEvent.getSource().getBindingContext().getObject();
			var aFiles = oItem.local_files || [];
			if (aFiles.length !== 1) {
				// Several files: pick one in the detail dialog (all links)
				this._openDetail(oItem.dockey);
				return;
			}
			// Single file: trigger the browser save dialog without leaving
			// the page.
			var oLink = document.createElement("a");
			oLink.href = "/api/objects/" + encodeURIComponent(oItem.dockey)
				+ "/files/" + encodeURIComponent(aFiles[0]);
			oLink.download = aFiles[0];
			document.body.appendChild(oLink);
			oLink.click();
			oLink.remove();
		},

		_openDetail: function (sDockey) {
			var that = this;
			Promise.all([
				Api.get("/api/objects/" + encodeURIComponent(sDockey)),
				Api.get("/api/objects/" + encodeURIComponent(sDockey) + "/protocol")
			]).then(function (aResults) {
				var oDetail = aResults[0], oProtocol = aResults[1];
				var oBundle = that._coreModel("i18n").getResourceBundle();
				var sStatus = (oDetail.status_text)
					|| (oProtocol.entries.length
						? oProtocol.entries[oProtocol.entries.length - 1].message
						: "");
				oDetail.size_text = oDetail.size_kb ? (oDetail.size_kb).toLocaleString() + " KB" : "";
				oDetail.files.forEach(function (f) {
					f.bytes_text = f.bytes_committed ? Api._formatBytes(f.bytes_committed) : "";
					f.url = f.status === "completed" && f.file_name
						? "/api/objects/" + encodeURIComponent(sDockey)
							+ "/files/" + encodeURIComponent(f.file_name)
						: "";
				});
				var sDesc = oDetail.description || "";
				oDetail.description_html = "<p>" + sDesc
					.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
					.replace(/\n/g, "<br/>") + "</p>";
				oDetail.hasFiles = oDetail.files.length > 0;
				oDetail.hasDescription = !!sDesc;
				oDetail.status_text = sStatus;
				oDetail.labels = {
					dockey: oBundle.getText("object"),
					docType: oBundle.getText("detailDocType"),
					size: oBundle.getText("size"),
					status: oBundle.getText("status"),
					files: oBundle.getText("detailFiles"),
					file: oBundle.getText("detailFile"),
					bytes: oBundle.getText("detailBytes"),
					fileStatus: oBundle.getText("status"),
					saveFile: oBundle.getText("saveFileTooltip"),
					description: oBundle.getText("detailDescription"),
					close: oBundle.getText("close")
				};
				return Fragment.load({
					name: "dlm.fragment.Detail", controller: that
				}).then(function (oDialog) {
					that.getView().addDependent(oDialog);
					oDialog.setModel(new JSONModel(oDetail), "dlg");
					oDialog.open();
				});
			}).catch(function (err) { MessageBox.error(err.message); });
		},

		onDeleteLocal: function () {
			var that = this;
			MessageBox.confirm(this._getText("confirmDeleteLocal"),
				{ title: this._getText("titleDeleteLocal"),
				  onClose: function (sAction) {
					if (sAction !== MessageBox.Action.OK) { return; }
					Promise.all(that._selectedKeys().map(function (k) {
						return Api.del("/api/downloads/" + encodeURIComponent(k) + "/local");
					})).then(function () {
						return Api.refreshBasket();
					}).then(function () { that._decorate(); })
					.catch(function (err) { that._showError(err.message); });
				}});
		},

		onRemoveFromBasket: function () {
			var that = this;
			MessageBox.confirm(this._getText("confirmRemoveBasket"),
				{ title: this._getText("titleRemoveBasket"),
				  onClose: function (sAction) {
					if (sAction !== MessageBox.Action.OK) { return; }
					Promise.all(that._selectedKeys().map(function (k) {
						return Api.del("/api/basket/" + encodeURIComponent(k));
					})).then(function () {
						return Api.refreshBasket();
					}).then(function () { that._decorate(); })
					.catch(function (err) { that._showError(err.message); });
				}});
		},

		// ------------------------------------------------------------- dialogs
		onHistory: function () {
			var that = this;
			Api.get("/api/history").then(function (oData) {
				Fragment.load({
					name: "dlm.fragment.History", controller: that
				}).then(function (oDialog) {
					that.getView().addDependent(oDialog);
					oDialog.setModel(new JSONModel({ entries: oData.entries }), "dlg");
					oDialog.open();
				});
			});
		},

		onSchedule: function () {
			var that = this;
			Fragment.load({
				name: "dlm.fragment.Schedule", controller: that
			}).then(function (oDialog) {
				that._oScheduleDialog = oDialog;
				that.getView().addDependent(oDialog);
				Api.get("/api/schedule").then(function (oSchedule) {
					oDialog.setModel(new JSONModel({
						schedule_at: oSchedule.schedule_at || "",
						schedule_mode: oSchedule.mode || "all"
					}), "dlg");
					oDialog.open();
				});
			});
		},

		onScheduleSave: function () {
			var oData = this._oScheduleDialog.getModel("dlg").getData();
			Api.post("/api/schedule", {
				schedule_at: oData.schedule_at || "",
				schedule_mode: oData.schedule_mode || "all"
			}).then(function () {
				MessageToast.show("Scheduled.");
				this._oScheduleDialog.close();
			}.bind(this)).catch(function (err) { MessageBox.error(err.message); });
		},

		onScheduleCancel: function () { this._oScheduleDialog.close(); },
		onDialogClose: function (oEvent) {
			// Buttons live in the Dialog's (begin|end)Button aggregation.
			var oSource = oEvent.getSource();
			var oDialog = oSource.getParent();
			if (oDialog && oDialog.close) { oDialog.close(); }
		},

		// ------------------------------------------------------- table helpers
		onSearch: function (oEvent) {
			this._sFilter = (oEvent.getParameter("newValue") || "").toLowerCase();
			this._applyFilterSort();
		},

		onTableUpdate: function () { /* reserved */ },

		onSelectAll: function () { this._oTable.selectAll(); },
		onDeselectAll: function () { this._oTable.removeSelections(); },

		_applyFilterSort: function () {
			if (!this._oTable.getBinding("items")) { return; }
			var sFilter = this._sFilter || "";
			var oList = this._coreModel().getProperty("/basket/items") || [];
			var aKeys = sFilter
				? oList.filter(function (o) {
					return (o.text || "").toLowerCase().indexOf(sFilter) !== -1 ||
						(o.dockey || "").indexOf(sFilter) !== -1;
				}).map(function (o) { return o.dockey; })
				: null;
			var oBinding = this._oTable.getBinding("items");
			var oFilter = aKeys
				? new Filter({
					filters: aKeys.map(function (k) {
						return new Filter("dockey", "EQ", k);
					}), bAnd: false })
				: [];
			oBinding.filter(oFilter);
			oBinding.sort(new Sorter(SORT_PATHS[this._oSorter.key] || "text",
				this._oSorter.descending));
		},

		onSortChange: function (oEvent) {
			this._oSorter.key = oEvent.getParameter("selectedItem").getKey();
			this._applyFilterSort();
		},

		onSortToggle: function () {
			this._oSorter.descending = !this._oSorter.descending;
			this.byId("sortDir").setIcon(this._oSorter.descending
				? "sap-icon://sort-descending" : "sap-icon://sort");
			this._applyFilterSort();
		},

		// ------------------------------------------------------------- misc UI
		onSettings: function () {
			var oApp = this.getView().getParent(); // sap.m.App
			oApp.to(oApp.getPages()[1]);
		},

		onThemeToggle: function () {
			var sCurrent = sap.ui.getCore().getConfiguration().getTheme();
			var sNext = sCurrent === "sap_horizon_dark" ? "sap_horizon" : "sap_horizon_dark";
			sap.ui.getCore().applyTheme(sNext);
			sap.ui.getCore().getModel().setProperty("/theme", sNext);
			window.localStorage.setItem("dlm.theme", sNext);
			// icon shows the mode you would switch to next
			this.byId("btnTheme").setIcon(
				sNext === "sap_horizon_dark"
					? "sap-icon://light-mode" : "sap-icon://dark-mode");
		},

		_setBusy: function (bBusy, sText) {
			var oModel = sap.ui.getCore().getModel();
			oModel.setProperty("/busy", bBusy);
			oModel.setProperty("/busyText", sText || "");
		},

		_showError: function (sMessage, sFallbackKey) {
			var sText;
			if (/HTTP\/1\.1 401|401/.test(sMessage)) { sText = this._getText("err401"); }
			else if (/403/.test(sMessage)) { sText = this._getText("err403"); }
			else if (/404/.test(sMessage)) { sText = this._getText("err404"); }
			else if (/503/.test(sMessage)) { sText = this._getText("err503"); }
			else { sText = sMessage || this._getText(
				sFallbackKey ? sFallbackKey : "errGeneral"); }
			MessageBox.error(sText);
		}
	});
});
