/*!
 * OpenUI5
 * (c) Copyright 2026 SAP SE or an SAP affiliate company.
 * Licensed under the Apache License, Version 2.0 - see LICENSE.txt.
 */
sap.ui.define(["sap/ui/base/Object","sap/ui/core/Element","sap/ui/test/RecordReplay"],function(e,t,n){"use strict";var o=e.extend("sap.ui.testrecorder.controlSelectors.ControlSelectorGenerator",{});o.prototype.getSelector=function(e){var t=r(e);return n.findControlSelectorByDOMElement({domElement:t,settings:e.settings}).then(function(e){return e})};function r(e){if(e.domElementId&&typeof e.domElementId==="string"){return document.getElementById(e.domElementId)}else if(e.controlId){return t.getElementById(e.controlId).getFocusDomRef()}}return new o});
//# sourceMappingURL=ControlSelectorGenerator.js.map