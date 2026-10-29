sap.ui.define([
	"sap/ui/core/mvc/Controller",
	"sap/m/MessageBox",
	"sap/m/MessageToast",
	"dlm/util/Api"
], function (Controller, MessageBox, MessageToast, Api) {
	"use strict";

	var MASK = "******";

	return Controller.extend("dlm.controller.Settings", {

		onInit: function () {
			this._bSecretsLoaded = false;
			this._oModel = sap.ui.getCore().getModel();
			this._load();
		},

		_coreModel: function (sName) {
			return sap.ui.getCore().getModel(sName);
		},

		_load: function () {
			var that = this;
			Promise.all([
				Api.get("/api/config"),
				Api.get("/api/credentials")
			]).then(function (aResults) {
				var oConfig = aResults[0], oCreds = aResults[1];
				// Secret fields start as the mask: sending it back keeps the value.
				oConfig.sap_password = oCreds.sap_password.stored ? MASK : "";
				oConfig.proxy_password = oCreds.proxy_password.stored ? MASK : "";
				oConfig.cert_key_password = oCreds.cert_key_password.stored ? MASK : "";
				oConfig.webhook_secret = oCreds.webhook_secret.stored ? MASK : "";
				oConfig.use_cert_auth_false = !oConfig.use_cert_auth;
				that._oModel.setProperty("/settings", oConfig);
				that._bSecretsLoaded = true;
			}).catch(function (err) {
				MessageBox.error(err.message);
			});
		},

		onBack: function () {
			var oApp = this.getView().getParent();
			oApp.back();
		},

		_collect: function () {
			var oS = this._oModel.getProperty("/settings");
			return {
				config: {
					sap_url: String(oS.sap_url || "").trim(), username: String(oS.username || "").trim(),
					use_cert_auth: oS.use_cert_auth, cert_path: String(oS.cert_path || "").trim(),
					use_proxy: oS.use_proxy, proxy_host: String(oS.proxy_host || "").trim(),
					proxy_port: parseInt(oS.proxy_port, 10) || 0,
					proxy_username: String(oS.proxy_username || "").trim(),
					download_store: oS.download_store,
					create_subdir: oS.create_subdir,
					save_description: oS.save_description,
					remember_credentials: oS.remember_credentials,
					autostart_download: oS.autostart_download,
					trace: oS.trace,
					max_concurrent_objects: parseInt(oS.max_concurrent_objects, 10) || 1,
					webhook_url: String(oS.webhook_url || "").trim()
				},
				credentials: {
					sap_password: oS.sap_password,
					proxy_password: oS.proxy_password,
					cert_key_password: oS.cert_key_password,
					webhook_secret: oS.webhook_secret
				}
			};
		},

		onSave: function () {
			if (!this._bSecretsLoaded) {
				// Never save from an unloaded form: empty fields would wipe
				// the stored configuration (guard against stale/failed loads).
				MessageBox.warning(this._coreModel("i18n")
					.getResourceBundle().getText("notLoadedYet"));
				this._load();
				return;
			}
			var that = this;
			var oData = this._collect();
			Api.put("/api/config", oData.config).then(function () {
				return Api.put("/api/credentials", oData.credentials);
			}).then(function () {
				MessageToast.show(that._coreModel("i18n")
					.getResourceBundle().getText("saved"));
				that._load();
				return Api.refreshBasket();
			}).catch(function (err) { MessageBox.error(err.message); });
		},

		onTestConnection: function () {
			if (!this._bSecretsLoaded) {
				MessageBox.warning(this._coreModel("i18n")
					.getResourceBundle().getText("notLoadedYet"));
				this._load();
				return;
			}
			var that = this;
			var oData = this._collect();
			// Persist first so the test uses exactly what the user sees.
			Api.put("/api/config", oData.config).then(function () {
				return Api.put("/api/credentials", oData.credentials);
			}).then(function () {
				return Api.post("/api/config/test");
			}).then(function (oResult) {
				if (oResult.ok) {
					MessageToast.show(oResult.message);
				} else {
					MessageBox.warning(oResult.message);
				}
			}).catch(function (err) { MessageBox.error(err.message); });
		}
	});
});
