<?php
/**
 * Plugin Name: Axis Flamingo REST API
 * Plugin URI:  https://github.com/axisindiacomponents-arch/Weeky-Leads-Report
 * Description: Exposes a REST endpoint for Flamingo inbound messages so that
 *              wp_fetch_leads.py can query CF7 submissions without needing
 *              CSV exports. Endpoint: GET /wp-json/axis/v1/leads
 * Version:     1.0.0
 * Author:      Axis Electricals (Digital)
 * Requires at least: 6.0
 * Requires PHP: 8.0
 *
 * =============================================================================
 * INSTALLATION
 * =============================================================================
 * 1. Copy this file to your WordPress plugins directory:
 *       wp-content/plugins/axis-flamingo-rest-api/axis-flamingo-rest-api.php
 *    (Create the folder axis-flamingo-rest-api/ first.)
 *
 * 2. In WordPress Admin → Plugins, activate "Axis Flamingo REST API".
 *
 * 3. Verify the endpoint is live:
 *       curl -u "Editor_account:APP_PASSWORD" \
 *            "https://axis-india.com/wp-json/axis/v1/leads?after=2026-03-06T00:00:00Z&before=2026-03-13T23:59:59Z"
 *
 * 4. The endpoint requires authentication (edit_posts capability).
 *    Use a WordPress Application Password — same credentials used by
 *    wp_fetch_leads.py via WORDPRESS_APP_PASSWORD env var.
 *
 * =============================================================================
 * ENDPOINT REFERENCE
 * =============================================================================
 * GET /wp-json/axis/v1/leads
 *
 * Query parameters:
 *   after    ISO-8601 datetime (UTC). Return submissions AFTER this time.
 *   before   ISO-8601 datetime (UTC). Return submissions BEFORE this time.
 *   channel  (optional) Filter by Flamingo channel name.
 *   per_page (optional) Items per page, default 100, max 200.
 *   page     (optional) Page number, default 1.
 *
 * Response: JSON array of objects, each with:
 *   id        int     — flamingo_inbound post ID
 *   date      string  — submission datetime (UTC, ISO-8601)
 *   channel   string  — CF7 form title stored by Flamingo
 *   subject   string  — email subject / entry title
 *   fields    object  — key→value map of all CF7 field values
 *
 * =============================================================================
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_action( 'rest_api_init', function () {
    register_rest_route( 'axis/v1', '/leads', [
        'methods'             => WP_REST_Server::READABLE,
        'callback'            => 'axis_flamingo_get_leads',
        'permission_callback' => function () {
            // Require authentication; restrict to users who can edit posts.
            return current_user_can( 'edit_posts' );
        },
        'args' => [
            'after' => [
                'description'       => 'Return leads submitted after this UTC datetime (ISO-8601).',
                'type'              => 'string',
                'format'            => 'date-time',
                'required'          => false,
                'sanitize_callback' => 'sanitize_text_field',
            ],
            'before' => [
                'description'       => 'Return leads submitted before this UTC datetime (ISO-8601).',
                'type'              => 'string',
                'format'            => 'date-time',
                'required'          => false,
                'sanitize_callback' => 'sanitize_text_field',
            ],
            'channel' => [
                'description'       => 'Filter by Flamingo channel name (CF7 form title).',
                'type'              => 'string',
                'required'          => false,
                'sanitize_callback' => 'sanitize_text_field',
            ],
            'per_page' => [
                'description'       => 'Number of items per page (max 200).',
                'type'              => 'integer',
                'default'           => 100,
                'minimum'           => 1,
                'maximum'           => 200,
                'sanitize_callback' => 'absint',
            ],
            'page' => [
                'description'       => 'Page number.',
                'type'              => 'integer',
                'default'           => 1,
                'minimum'           => 1,
                'sanitize_callback' => 'absint',
            ],
        ],
    ] );
} );


/**
 * Callback: query flamingo_inbound posts and return normalised JSON.
 *
 * @param WP_REST_Request $request
 * @return WP_REST_Response
 */
function axis_flamingo_get_leads( WP_REST_Request $request ): WP_REST_Response {
    $after    = $request->get_param( 'after' );
    $before   = $request->get_param( 'before' );
    $channel  = $request->get_param( 'channel' );
    $per_page = (int) $request->get_param( 'per_page' );
    $page     = (int) $request->get_param( 'page' );

    // -------------------------------------------------------------------
    // Build WP_Query args
    // -------------------------------------------------------------------
    $query_args = [
        'post_type'      => 'flamingo_inbound',
        'post_status'    => 'any',
        'posts_per_page' => $per_page,
        'paged'          => $page,
        'orderby'        => 'date',
        'order'          => 'DESC',
    ];

    // Date range via date_query (WordPress stores post dates in local time,
    // but Flamingo uses UTC for its meta; we use post_date_gmt for accuracy).
    $date_query = [ 'inclusive' => true ];

    if ( $after ) {
        $date_query['after'] = $after;
    }
    if ( $before ) {
        $date_query['before'] = $before;
    }
    if ( $after || $before ) {
        $date_query['column']     = 'post_date_gmt';
        $query_args['date_query'] = [ $date_query ];
    }

    // Channel filter via tax_query (Flamingo stores channels as a taxonomy)
    if ( $channel ) {
        $query_args['tax_query'] = [
            [
                'taxonomy' => 'flamingo_inbound_channel',
                'field'    => 'name',
                'terms'    => $channel,
            ],
        ];
    }

    $query = new WP_Query( $query_args );

    // Expose pagination headers (mirrors WP REST convention)
    $total       = (int) $query->found_posts;
    $total_pages = (int) $query->max_num_pages;

    $response_data = [];

    foreach ( $query->posts as $post ) {
        $response_data[] = axis_flamingo_format_post( $post );
    }

    $response = new WP_REST_Response( $response_data, 200 );
    $response->header( 'X-WP-Total',      $total );
    $response->header( 'X-WP-TotalPages', $total_pages );

    return $response;
}


/**
 * Convert a flamingo_inbound WP_Post into the normalised array returned
 * by the API endpoint.
 *
 * @param WP_Post $post
 * @return array
 */
function axis_flamingo_format_post( WP_Post $post ): array {
    // Channel name from taxonomy
    $channel_terms = wp_get_post_terms( $post->ID, 'flamingo_inbound_channel' );
    $channel       = ( ! is_wp_error( $channel_terms ) && ! empty( $channel_terms ) )
        ? $channel_terms[0]->name
        : '';

    // Flamingo stores all CF7 field values serialised in post meta _fields
    $raw_fields  = get_post_meta( $post->ID, '_fields', true );
    $fields_flat = [];

    if ( is_array( $raw_fields ) ) {
        foreach ( $raw_fields as $field ) {
            // Each entry is typically ['name' => '...', 'value' => '...']
            if ( isset( $field['name'], $field['value'] ) ) {
                $fields_flat[ $field['name'] ] = $field['value'];
            }
        }
    }

    // Normalise the Location / country field to a predictable key
    $location = '';
    foreach ( [ 'location', 'Location', 'country', 'Country', 'your-location' ] as $key ) {
        if ( ! empty( $fields_flat[ $key ] ) ) {
            $location = $fields_flat[ $key ];
            break;
        }
    }

    // Page title / catalogue name — used for ebook categorisation
    $pagetitle = '';
    foreach ( [ 'pagetitle', 'page-title', 'Pagetitle', 'catalogue', 'subject' ] as $key ) {
        if ( ! empty( $fields_flat[ $key ] ) ) {
            $pagetitle = $fields_flat[ $key ];
            break;
        }
    }

    return [
        'id'        => $post->ID,
        'date'      => get_gmt_from_date( $post->post_date, 'c' ),  // UTC ISO-8601
        'channel'   => $channel,
        'subject'   => $post->post_title,
        'location'  => $location,
        'pagetitle' => $pagetitle,
        'fields'    => $fields_flat,
    ];
}
