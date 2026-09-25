<#-- Keycloak's base info.ftl, changed in one place: when Keycloak hides the
     link (skipLink, e.g. after a password reset opened from the email in a new
     session, which deliberately does not log the person in), still offer the
     way back to the application, whose start page begins a normal login.
     Re-check against theme/base/login/info.ftl on every Keycloak upgrade. -->
<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <#if messageHeader??>
            ${kcSanitize(msg("${messageHeader}"))?no_esc}
        <#else>
            ${message.summary}
        </#if>
    <#elseif section = "form">
    <div id="kc-info-message">
        <p class="instruction">${message.summary}<#if requiredActions??><#list requiredActions>: <b><#items as reqActionItem>${kcSanitize(msg("requiredAction.${reqActionItem}"))?no_esc}<#sep>, </#items></b></#list><#else></#if></p>
        <#if pageRedirectUri?has_content && !(skipLink??)>
            <p><a href="${pageRedirectUri}">${msg("backToApplication")}</a></p>
        <#elseif actionUri?has_content && !(skipLink??)>
            <p><a href="${actionUri}">${msg("proceedWithAction")}</a></p>
        <#elseif (client.baseUrl)?has_content>
            <p><a href="${client.baseUrl}">${msg("backToApplication")}</a></p>
        </#if>
    </div>
    </#if>
</@layout.registrationLayout>
