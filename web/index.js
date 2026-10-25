sap.ui.define([
	"sap/ui/core/mvc/XMLView",
	"sap/ui/model/json/JSONModel",
	"sap/ui/model/resource/ResourceModel",
	"dlm/util/Api"
], function (XMLView, JSONModel, ResourceModel, Api) {
	"use strict";

	// NOTE: executed via data-sap-ui-onInit="module:dlm/index", i.e. AFTER the
	// core is initialized - so run directly, do not attach to the init event.

	// i18n (Fiori guideline: no hard-coded UI texts)
	sap.ui.getCore().setModel(
		new ResourceModel({ bundleName: "dlm.i18n.i18n" }), "i18n");

	// Global model: status, basket, progress, settings
	var oModel = new JSONModel({
		status: { configured: false, connected: false, downloading: [] },
		basket: { refreshed: false, full_name: "", items: [], upgrade: null },
		progress: {},
		settings: {},
		credentials: {},
		footer: { icon: "", connectionText: "", downloadingText: "" },
		theme: "sap_horizon"
	});
	oModel.setSizeLimit(1000);
	sap.ui.getCore().setModel(oModel);

	// Theme (light/dark) persisted per user; OS preference as first-run default.
	var sTheme = window.localStorage.getItem("dlm.theme");
	if (!sTheme) {
		sTheme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
			? "sap_horizon_dark" : "sap_horizon";
	}
	sap.ui.getCore().applyTheme(sTheme);
	oModel.setProperty("/theme", sTheme);

	// Live events: progress / status / error
	Api.connectEvents(oModel);

	XMLView.create({ viewName: "dlm.view.App" }).then(function (oView) {
		oView.placeAt("content");
	});
});
